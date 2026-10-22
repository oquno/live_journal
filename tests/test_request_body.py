import io
import unittest
from unittest.mock import Mock, patch

import app


class RequestBodyTests(unittest.TestCase):
    def request(self, body=b'', length=None, stream=None):
        result = {}
        env = {'PATH_INFO': '/login', 'REQUEST_METHOD': 'POST',
               'CONTENT_LENGTH': str(len(body)) if length is None else length,
               'wsgi.input': stream if stream is not None else io.BytesIO(body)}
        def start(status, headers):
            result['status'] = status
        result['body'] = b''.join(app.application(env, start))
        return result

    def test_rejects_oversize_before_reading(self):
        for length in (str(app.MAX_FORM_BYTES + 1), '9' * 5000):
            stream = Mock()
            self.assertEqual(self.request(length=length, stream=stream)['status'], '413 Content Too Large')
            stream.read.assert_not_called()

    def test_rejects_invalid_and_missing_lengths_before_reading(self):
        for length, expected in [('-1', '400 Bad Request'), ('abc', '400 Bad Request'),
                                 ('１', '400 Bad Request'), ('', '411 Length Required')]:
            stream = Mock()
            self.assertEqual(self.request(length=length, stream=stream)['status'], expected)
            stream.read.assert_not_called()

    def test_rejects_bad_encoding_truncated_body_and_excess_fields(self):
        for body, length in [(b'password=\xff', None), (b'password=%FF', None),
                             (b'a=1', '10'), (b'&'.join([b'a=1'] * (app.MAX_FORM_FIELDS + 1)), None)]:
            self.assertEqual(self.request(body, length)['status'], '400 Bad Request')

    def test_accepts_limits_and_repeated_fields(self):
        raw = b'a=' + b'x' * (app.MAX_FORM_BYTES - 2)
        self.assertEqual(len(app.parse_body({'CONTENT_LENGTH': str(len(raw)), 'wsgi.input': io.BytesIO(raw)})['a'][0]), app.MAX_FORM_BYTES - 2)
        raw = b'&'.join([b'a=1'] * app.MAX_FORM_FIELDS)
        self.assertEqual(len(app.parse_body({'CONTENT_LENGTH': str(len(raw)), 'wsgi.input': io.BytesIO(raw)})['a']), app.MAX_FORM_FIELDS)

    def test_normal_login_still_reaches_authentication(self):
        with patch.object(app, 'ADMIN_PASSWORD', 'test-password'), patch.object(app, 'SESSION_SECRET', 'test-secret'):
            self.assertEqual(self.request(b'username=admin&password=test-password')['status'], '303 See Other')
