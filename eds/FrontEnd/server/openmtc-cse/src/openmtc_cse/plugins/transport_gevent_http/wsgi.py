from _socket import gaierror
from operator import itemgetter
from socket import AF_INET, AF_INET6, getaddrinfo, SOCK_STREAM, inet_pton
from werkzeug.wrappers import (BaseRequest, CommonRequestDescriptorsMixin,
                               UserAgentMixin, AcceptMixin, Response)

from funcy import pluck

from futile.collections import get_iterable
from futile.logging import LoggerMixin
from openmtc_cse.methoddomain.filtercriteria import parse_filter_criteria
from openmtc_onem2m.exc import (CSEError, CSEContentsUnacceptable,
                                STATUS_INTERNAL_SERVER_ERROR, CSEBadRequest)
from openmtc_onem2m.model import (Notification, AttributeList,
                                  get_long_attribute_name)
from openmtc_onem2m.serializer import get_onem2m_supported_content_types
from openmtc_onem2m.serializer.util import (decode_onem2m_content,
                                            encode_onem2m_content)
from openmtc_onem2m.transport import (OneM2MErrorResponse, OneM2MOperation,
                                      OneM2MRequest)
import urlparse
from openmtc_onem2m.model import get_long_member_name

_method_map_from_http = {
    'POST': OneM2MOperation.create,
    'GET': OneM2MOperation.retrieve,
    'PUT': OneM2MOperation.update,
    'DELETE': OneM2MOperation.delete
}


def is_ipv4(address):
    try:
        inet_pton(AF_INET, address)
    except:
        return False
    return True


def is_ipv6(address):
    try:
        inet_pton(AF_INET6, address)
    except:
        return False
    return True


class Request(BaseRequest, CommonRequestDescriptorsMixin, UserAgentMixin,
              AcceptMixin):
    pass


class OpenMTCWSGIApplication(LoggerMixin):
    __cached_addresses = {}

    def __init__(self, request_handler, server_address, default_content_type,
                 pretty=False):
        super(OpenMTCWSGIApplication, self).__init__()

        self.request_handler = request_handler
        self.__cache = set()

        if server_address == "::":
            self.__addresses = self._get_addresses(AF_INET6) | \
                               self._get_addresses(AF_INET)
            self._resolve_host = self._resolve_host_ipv6
        elif server_address in ("", "0.0.0.0"):
            self.__addresses = self._get_addresses(AF_INET)
        else:
            self.__addresses = get_iterable(server_address)

        self.logger.debug("All listening addresses: %s", self.__addresses)

        self.default_content_type = default_content_type
        self.pretty = pretty

    def _get_addresses(self, family):
        try:
            return self.__cached_addresses[family]
        except KeyError:
            from netifaces import interfaces, ifaddresses

            addresses = self.__cached_addresses[family] = set()

            for interface in interfaces():
                try:
                    ifdata = ifaddresses(interface)[family]
                    ifaddrs = map(lambda x: x.split("%")[0], pluck("addr",
                                                                   ifdata))
                    addresses.update(ifaddrs)
                except KeyError:
                    pass

            return addresses

    def _get_addr_info(self, host, family):
        self.logger.debug("Resolving %s", host)
        try:
            info = getaddrinfo(host, 0, family, SOCK_STREAM)
            return set(map(itemgetter(0), map(itemgetter(-1), info)))
        except gaierror as e:
            self.logger.error("Failed to resolve %s: %s", host, e)
            return set()

    def _resolve_host(self, host):
        if is_ipv4(host):
            return {host}
        return self._get_addr_info(host, AF_INET)

    def _resolve_host_ipv6(self, host):
        self.logger.debug("Resolving: %s", host)
        if is_ipv6(host):
            return {host}
        # TODO: kca: optimize
        return (self._get_addr_info(host, AF_INET) |
                self._get_addr_info(host, AF_INET6))

    def __call__(self, environ, start_response):
        with Request(environ) as request:
            response = (self._handle_options(request)
                        if request.method == "OPTIONS"
                        else self._handle_http_request(request))
            return response(environ, start_response)

    @staticmethod
    def _handle_options(request):
        # TODO: use full list of supported encodings
        accept = request.accept_mimetypes
        if not ('*/*' in accept or not accept or
                'application/json' in accept or
                'application/xml' in accept):
            return Response('Only application/json and '
                            'application/xml are supported.',
                            status=400)
        if not ('application/json' in accept or
                'application/xml' in accept):
            return Response('Only application/json and '
                            'application/xml are supported.',
                            status=405)
        return Response(
            status=204
        )

    def map_http_request_to_onem2m_request(self, http_request):
        """Maps a HTTP request to a OneM2M request.

        :param http_request: the HTTP Request object to be mapped
        :returns: OneM2MRequest -- the resulting OneM2M request object
        :raises: OpenMTCError
        """
        self.logger.debug("Mapping HTTP request '%s' to OneM2M request",
                          http_request)

        op = _method_map_from_http[http_request.method]
        to = http_request.path[1:].lstrip('/')
        if to.startswith('~/'):
            to = to[1:]
        elif to.startswith('_/'):
            to = '/' + to[1:]

        get_header = http_request.headers.get

        # The X-M2M-Origin header shall be mapped to the From parameter of
        # request and response primitives and vice versa, if applicable.
        fr = get_header("x-m2m-origin")

        # The X-M2M-RI header shall be mapped to the Request Identifier
        # parameter of request and response primitives and vice versa.
        rqi = get_header("x-m2m-ri")

        # primitive content
        pc = decode_onem2m_content(http_request.input_stream.read(),
                                   http_request.content_type)

        # resource type
        # get out of content-type or from resource
        ty = type(pc) if pc else None
        if ty is Notification:
            op = OneM2MOperation.notify

        # The X-M2M-GID header shall be mapped to the Group Request Identifier
        # parameter of request primitives and vice versa, if applicable.
        gid = get_header("x-m2m-gid")

        # The X-M2M-RTU header shall be mapped to the notificationURI element of
        # the Response Type parameter of request primitives and vice versa, if
        # applicable. If there are more than one value in the element, then the
        # values shall be combined with "&" character.
        rt = get_header("x-m2m-rtu")

        # The X-M2M-OT header shall be mapped to the Originating Timestamp
        # parameter of request and response primitives, and vice versa, if
        # applicable.
        ot = get_header("x-m2m-ot")

        # The X-M2M-RST header shall be mapped to the Result Expiration
        # Timestamp parameter of request and response primitives, and vice
        # versa, if applicable.
        rset = get_header("x-m2m-rst")

        # The X-M2M-RET header shall be mapped to the Request Expiration
        # Timestamp parameter of request primitives and vice versa, if
        # applicable.
        rqet = get_header("x-m2m-ret")

        # The X-M2M-OET header shall be mapped to the Operation Execution Time
        # parameter of request primitives and vice versa, if applicable
        oet = get_header("x-m2m-oet")

        # The X-M2M-EC header shall be mapped to the Event Category parameter of
        #  request and response primitives, and vice versa, if applicable.
        ec = get_header("x-m2m-ec")

        onem2m_request = OneM2MRequest(op=op, to=to, fr=fr, rqi=rqi, ty=ty,
                                       pc=pc, ot=ot, rqet=rqet, rset=rset,
                                       oet=oet, rt=rt, ec=ec, gid=gid)

        not_filter_params = ('rt', 'rp', 'rcn', 'da', 'drt')
        multiple_params = ('lbl', 'ty', 'cty', 'atr')

        if http_request.query_string:
            from openmtc_cse.methoddomain.filtercriteria import filters
            params = urlparse.parse_qs(http_request.query_string)
            get_param = params.get
            f_c = {}

            for param in params:
                self.logger.debug("checking '%s'", param)

                values = get_param(param)

                if param not in multiple_params and len(values) > 1:
                    raise CSEBadRequest("Multiple field names not permitted "
                                        "for parameter %s" % param)

                param_long_name = get_long_member_name(param)

                # TODO(rst): handle attributes with get_long_attribute_name
                if param in not_filter_params:
                    setattr(onem2m_request, param, values[0])
                elif param_long_name == 'attributeList':
                    onem2m_request.pc = AttributeList(
                        map(get_long_attribute_name, values[0].split(' ')))
                elif param_long_name and hasattr(filters, param_long_name):
                    self.logger.debug("got values for '%s' ('%s'): %s",
                                      param_long_name, param, values)

                    if param in multiple_params:
                        f_c[param_long_name] = values
                    else:
                        f_c[param_long_name] = values[0]
                else:
                    raise CSEBadRequest("Unknown parameter: %s" % param)
            onem2m_request.filter_criteria = parse_filter_criteria(f_c)

        return onem2m_request

    def map_onem2m_response_to_http_response(self, request, response):
        """Maps a OneM2M response to a HTTP response.

        :param request: the HTTP request
        :param response: the OneM2MResponse object to be mapped
        :returns: Response -- the resulting HTTP Response
        :raises: OpenMTCError
        """
        self.logger.debug("Mapping OneM2M response: %s", response)

        status_code = response.status_code

        # resourceID prefix
        if response.to.startswith('//'):
            resource_id_pre = '/'.join(response.to.split('/')[:4]) + '/'
        elif response.to.startswith('/'):
            resource_id_pre = '/'.join(response.to.split('/')[:2]) + '/'
        else:
            resource_id_pre = ''

        headers = {
            "x-m2m-ri": str(response.rqi),
            "x-m2m-rsc": str(response.rsc)
        }

        try:
            headers['content_location'] = (resource_id_pre +
                                           response.content.resourceID)
        except (AttributeError, TypeError):
            pass

        pretty = self.pretty
        if pretty is None:
            user_agent = request.user_agent
            pretty = user_agent and ("opera" in user_agent or
                                     "mozilla" in user_agent)
        supported = get_onem2m_supported_content_types()
        if request.accept_mimetypes:
            accept = request.accept_mimetypes.best_match(supported)
            if accept is None:
                # TODO(rst): raise 406 or similar
                accept = self.default_content_type
        else:
            accept = self.default_content_type
        try:
            content_type, payload = encode_onem2m_content(response.content,
                                                          accept, pretty,
                                                          path=resource_id_pre)
        except CSEContentsUnacceptable as e:
            status_code = e.status_code
            content_type = "text/plain"
            payload = str(e)

        response = Response(payload,
                            status=status_code,
                            headers=headers,
                            content_type=content_type)
        return response

    def map_onem2m_error_to_http_error(self, response):
        """Maps a OneM2M error response to a HTTP response.

        :param response: the OneM2MResponse object to be mapped
        :returns: Response -- the resulting Response object
        :raises: OpenMTCError
        """
        self.logger.debug("Mapping OneM2M error: %s", response)
        # This is strange, rs is defined as a string (Okay, Not Okay), and
        # status codes are optional in onem2m req

        # TODO(rst): handle this better
        try:
            status_code = response.status_code

            headers = {
                "x-m2m-ri": str(response.rqi),
                "x-m2m-rsc": str(response.rsc)
            }
        except AttributeError:
            status_code = STATUS_INTERNAL_SERVER_ERROR.http_status_code
            headers = None

        return Response(status=status_code, headers=headers)

    def _handle_http_request(self, request):
        onem2m_request = OneM2MRequest(None, None)

        try:
            onem2m_request = self.map_http_request_to_onem2m_request(request)
            onem2m_response = self.request_handler(onem2m_request).get()
        except OneM2MErrorResponse as error_response:
            response = self.map_onem2m_error_to_http_error(error_response)
        except CSEError as error:
            error_response = OneM2MErrorResponse(error.response_status_code,
                                                 onem2m_request)
            response = self.map_onem2m_error_to_http_error(error_response)
        else:
            response = self.map_onem2m_response_to_http_response(
                request, onem2m_response)

        return response
