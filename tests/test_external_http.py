import io
import socket
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

import app
import external_http


def answer(ip='93.184.216.34', port=80):
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return [(family, socket.SOCK_STREAM, 6, '', sockaddr)]


class ExternalHTTPTests(unittest.TestCase):
    def test_rebinding_between_precheck_and_connect_is_blocked(self):
        with patch.object(socket, 'getaddrinfo', side_effect=[answer(), answer('127.0.0.1')]), \
                patch.object(socket, 'socket') as create:
            with self.assertRaises(ValueError):
                app.fetch_external_bytes('http://feed.example/rss')
            create.assert_not_called()

    def test_connection_uses_checked_numeric_address_without_resolving_again(self):
        sock = Mock()
        with patch.object(socket, 'getaddrinfo', side_effect=[answer(), answer('127.0.0.1')]) as dns, \
                patch.object(socket, 'socket', return_value=sock):
            self.assertIs(external_http.connect_public(('feed.example', 80), 10), sock)
            dns.assert_called_once_with('feed.example', 80, type=socket.SOCK_STREAM)
            sock.connect.assert_called_once_with(('93.184.216.34', 80))

    def test_private_shared_local_and_mixed_addresses_are_rejected(self):
        for ip in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '100.64.0.1',
                   '::1', 'fd00::1', '::ffff:127.0.0.1', '224.0.0.1']:
            with self.subTest(ip=ip), patch.object(socket, 'getaddrinfo', return_value=answer() + answer(ip)):
                with self.assertRaises(ValueError):
                    external_http.public_addresses('feed.example', 80)

    def test_public_feed_download_retains_original_host(self):
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\n<rss />')
        with patch.object(socket, 'getaddrinfo', return_value=answer()), patch.object(socket, 'socket', return_value=sock):
            self.assertEqual(app.fetch_external_bytes('http://feed.example/rss'), b'<rss />')
        self.assertIn(b'Host: feed.example', sock.sendall.call_args_list[0].args[0])

    def test_redirect_to_private_address_is_rejected(self):
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n')
        with patch.object(socket, 'getaddrinfo', side_effect=[answer(), answer(), answer('127.0.0.1')]), \
                patch.object(socket, 'socket', return_value=sock):
            with self.assertRaises(ValueError):
                app.fetch_external_bytes('http://feed.example/rss')

    def test_https_keeps_certificate_verification_and_original_sni(self):
        conn = external_http.PublicHTTPSConnection('feed.example')
        self.assertTrue(conn._context.check_hostname)
        self.assertEqual(conn._context.verify_mode, ssl.CERT_REQUIRED)
        raw_socket = Mock()
        with patch.object(external_http, 'connect_public', return_value=raw_socket), \
                patch.object(conn._context, 'wrap_socket', side_effect=ssl.SSLCertVerificationError('untrusted')) as wrap:
            with self.assertRaises(ssl.SSLCertVerificationError):
                conn.connect()
            wrap.assert_called_once_with(raw_socket, server_hostname='feed.example')

    def test_timeout_and_source_address_are_preserved(self):
        sock = Mock()
        with patch.object(socket, 'getaddrinfo', return_value=answer('2606:4700:4700::1111', 443)), \
                patch.object(socket, 'socket', return_value=sock):
            external_http.connect_public(('feed.example', 443), 5, ('::', 0))
        sock.settimeout.assert_called_once_with(5)
        sock.bind.assert_called_once_with(('::', 0))
        sock.connect.assert_called_once_with(('2606:4700:4700::1111', 443, 0, 0))
