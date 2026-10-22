import io
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import app
from login_throttle import MAX_ATTEMPTS, WINDOW_SECONDS, reserve_attempt


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name, 'journal.db'))
        with sqlite3.connect(self.path) as conn:
            conn.executescript(Path(app.BASE_DIR, 'schema.sql').read_text())

    def attempt(self, address='client', now=1000):
        conn = sqlite3.connect(self.path)
        try:
            return reserve_attempt(conn, address, now)
        finally:
            conn.close()

    def test_limit_survives_connections_and_expires(self):
        for _ in range(MAX_ATTEMPTS):
            self.assertEqual(self.attempt(), 0)
        self.assertEqual(self.attempt(), WINDOW_SECONDS)
        self.assertEqual(self.attempt(address='other'), 0)
        self.assertEqual(self.attempt(now=1000 + WINDOW_SECONDS), 0)

    def test_parallel_workers_cannot_exceed_limit(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.attempt(), range(24)))
        self.assertEqual(results.count(0), MAX_ATTEMPTS)

    def test_http_limit_ignores_spoofed_forwarded_addresses(self):
        with patch.object(app, 'DB_PATH', self.path), patch.object(app, 'ADMIN_PASSWORD', 'test-only'):
            for index in range(MAX_ATTEMPTS + 1):
                body = b'username=admin&password=wrong'
                captured = {}
                def start(status, headers):
                    captured.update(status=status, headers=dict(headers))
                env = {'PATH_INFO': '/login', 'REQUEST_METHOD': 'POST', 'REMOTE_ADDR': '127.0.0.1',
                       'HTTP_X_FORWARDED_FOR': str(index), 'CONTENT_LENGTH': str(len(body)),
                       'wsgi.input': io.BytesIO(body)}
                b''.join(app.application(env, start))
                self.assertEqual(captured['status'], '200 OK' if index < MAX_ATTEMPTS else '429 Too Many Requests')
            self.assertGreater(int(captured['headers']['Retry-After']), 0)
