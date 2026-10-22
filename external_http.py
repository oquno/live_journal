"""Direct HTTP(S) connections pinned to DNS answers checked before connecting."""
import http.client
import ipaddress
import socket
from urllib.request import HTTPHandler, HTTPSHandler


def public_addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(info[4][0]).is_global
                            or ipaddress.ip_address(info[4][0]).is_multicast
                            for info in addresses):
        raise ValueError("外部 URL は公開 IP アドレスである必要があります。")
    return addresses


def connect_public(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    # Never give the hostname to create_connection: that would resolve it again.
    addresses = public_addresses(*address)
    last_error = None
    for family, socktype, proto, _, sockaddr in addresses:
        connection = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
    raise last_error


class PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self._create_connection = connect_public
        super().connect()


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self._create_connection = connect_public
        # HTTPSConnection retains the original hostname for SNI and verification.
        super().connect()


class PublicHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(PublicHTTPConnection, request)


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PublicHTTPSConnection, request, context=self._context)
