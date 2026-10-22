import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import app
import session_store


class SessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name, 'journal.db'))
        self.enterContext(patch.multiple(app, DB_PATH=self.path, SESSION_SECRET='test-secret',
                                        ADMIN_USER='admin', ADMIN_PASSWORD='test-password'))
        with sqlite3.connect(self.path) as conn:
            conn.executescript(Path(app.BASE_DIR, 'schema.sql').read_text())

    def test_logout_invalidates_replayed_cookie_across_connections(self):
        token = app.sign_session('admin')
        env = {'HTTP_COOKIE': 'live_journal_session=' + token}
        body = urlencode({'csrf_token': app.csrf_token(env)}).encode()
        env.update(PATH_INFO='/logout', REQUEST_METHOD='POST', CONTENT_LENGTH=str(len(body)))
        env['wsgi.input'] = io.BytesIO(body)
        response = {}
        b''.join(app.application(env, lambda status, headers: response.update(status=status)))
        self.assertEqual(response['status'], '303 See Other')
        self.assertIsNone(app.verify_session(token))
        token2 = app.sign_session('admin')
        self.assertNotEqual(token, token2)
        self.assertIsNone(app.verify_session(token))
        self.assertEqual(app.verify_session(token2), 'admin')

    def test_credential_changes_invalidate_sessions_permanently(self):
        for field, value in [('ADMIN_PASSWORD', 'new-password'), ('ADMIN_PASSWORD', None),
                             ('ADMIN_USER', 'new-admin'), ('SESSION_SECRET', 'new-secret')]:
            with self.subTest(field=field, value=value):
                token = app.sign_session('admin')
                with patch.object(app, field, value):
                    self.assertIsNone(app.verify_session(token))
                self.assertIsNone(app.verify_session(token))

    def test_expiry_tampering_and_legacy_cookie(self):
        with patch.object(session_store.time, 'time', return_value=1000):
            token = app.sign_session('admin')
        with patch.object(session_store.time, 'time', return_value=1000 + session_store.SESSION_SECONDS - 1):
            self.assertEqual(app.verify_session(token), 'admin')
            self.assertIsNone(app.verify_session(token[:-1] + ('A' if token[-1] != 'A' else 'B')))
            self.assertIsNone(app.verify_session('admin:' + '0' * 64))
        with patch.object(session_store.time, 'time', return_value=1000 + session_store.SESSION_SECONDS):
            self.assertIsNone(app.verify_session(token))

    def test_tokens_are_unique_and_not_stored_in_plaintext(self):
        first, second = app.sign_session('admin'), app.sign_session('admin')
        self.assertNotEqual(first, second)
        with sqlite3.connect(self.path) as conn:
            hashes = [r[0] for r in conn.execute('SELECT token_hash FROM auth_sessions')]
        self.assertNotIn(first, hashes)
        self.assertNotIn(second, hashes)
        self.assertEqual(len(hashes), 2)

    def test_logout_without_csrf_keeps_session(self):
        token = app.sign_session('admin')
        env = {'PATH_INFO': '/logout', 'REQUEST_METHOD': 'POST', 'CONTENT_LENGTH': '0',
               'wsgi.input': io.BytesIO(), 'HTTP_COOKIE': 'live_journal_session=' + token}
        response = {}
        b''.join(app.application(env, lambda status, headers: response.update(status=status)))
        self.assertEqual(response['status'], '403 Forbidden')
        self.assertEqual(app.verify_session(token), 'admin')
