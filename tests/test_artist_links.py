import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlencode

import app


class ArtistLinksTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name, "journal.sqlite"))
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(Path(app.BASE_DIR, "schema.sql").read_text())
        self.name = '青葉 & AC/DC + "Live"'
        self.conn.execute("INSERT INTO artists (id, name, slug) VALUES (1, ?, 'artist')", (self.name,))
        self.conn.commit()

    def request(self, path="/artists/1", params=None, authenticated=False, csrf=True):
        captured = {}

        def start_response(status, headers):
            captured.update(status=status, headers=dict(headers))

        environ = {"PATH_INFO": path, "REQUEST_METHOD": "GET", "QUERY_STRING": ""}
        with patch.multiple(app, DB_PATH=self.path, APP_MODE="public", SESSION_SECRET="test-secret"):
            if authenticated:
                environ["HTTP_COOKIE"] = "live_journal_session=" + app.sign_session(app.ADMIN_USER)
            if params is not None:
                params = dict(params)
                if csrf:
                    params["csrf_token"] = app.csrf_token(environ)
                payload = urlencode(params).encode()
                environ.update(REQUEST_METHOD="POST", CONTENT_LENGTH=str(len(payload)))
                environ["wsgi.input"] = io.BytesIO(payload)
            captured["body"] = b"".join(app.application(environ, start_response)).decode()
        return captured

    def set_accounts(self, lastfm="listener", flickr="12345@N00"):
        app.set_app_setting(self.conn, "lastfm_user", lastfm)
        app.set_app_setting(self.conn, "flickr_user", flickr)
        self.conn.commit()

    def test_artist_links_use_personal_accounts_and_encode_names(self):
        self.set_accounts()
        for path in ("/artists/1", "/artists/artist"):
            with self.subTest(path=path):
                response = self.request(path)
                self.assertEqual(response["status"], "200 OK")
                self.assertIn('href="https://www.last.fm/user/listener/library/music/' + quote(self.name, safe="") + '"', response["body"])
                self.assertIn('href="https://www.flickr.com/photos/12345%40N00/tags/' + quote("青葉acdclive") + '/"', response["body"])
                self.assertIn('rel="noopener noreferrer"', response["body"])
                self.assertIn("Last.fm の scrobble 履歴", response["body"])

    def test_unconfigured_accounts_hide_links(self):
        body = self.request()["body"]
        self.assertNotIn("https://www.last.fm/user/", body)
        self.assertNotIn("https://www.flickr.com/photos/", body)

    def test_flickr_uses_enabled_sources_without_duplicates(self):
        self.conn.executemany(
            "INSERT INTO link_sources (kind, name, account_url, flickr_user_id, enabled) VALUES ('flickr', 'photos', ?, ?, ?)",
            [("https://www.flickr.com/photos/photographer/", "", 1),
             ("https://www.flickr.com/photos/photographer/", "", 1),
             ("", "12345@N00", 1),
             ("https://www.flickr.com/photos/disabled/", "", 0)],
        )
        self.conn.commit()
        body = self.request()["body"]
        self.assertEqual(body.count('href="https://www.flickr.com/photos/photographer/tags/'), 1)
        self.assertIn('href="https://www.flickr.com/photos/12345%40N00/tags/', body)
        self.assertNotIn("/photos/disabled/", body)
        self.set_accounts(flickr="chosen")
        body = self.request()["body"]
        self.assertIn("/photos/chosen/tags/", body)
        self.assertNotIn("/photos/photographer/", body)

    def test_settings_save_display_and_clear_accounts(self):
        response = self.request("/settings/artist-links", {"lastfm_user": " listener ", "flickr_user": "12345@N00"}, authenticated=True)
        self.assertEqual(response["status"], "303 See Other")
        self.assertEqual(app.get_app_setting(self.conn, "lastfm_user"), "listener")
        body = self.request("/settings/links", authenticated=True)["body"]
        self.assertIn('name="lastfm_user" value="listener"', body)
        self.assertIn('name="flickr_user" value="12345@N00"', body)
        self.request("/settings/artist-links", {"lastfm_user": "", "flickr_user": ""}, authenticated=True)
        self.assertNotIn("https://www.last.fm/user/", self.request()["body"])

    def test_settings_require_authentication_and_csrf(self):
        params = {"lastfm_user": "other", "flickr_user": "other"}
        self.assertEqual(self.request("/settings/artist-links", params)["status"], "303 See Other")
        self.assertEqual(self.request("/settings/artist-links", params, authenticated=True, csrf=False)["status"], "403 Forbidden")
        self.assertEqual(app.get_app_setting(self.conn, "lastfm_user"), "")

    def test_invalid_settings_preserve_existing_accounts(self):
        self.set_accounts()
        for value in ("https://www.last.fm/user/other", "two users", "..", "other?query"):
            response = self.request("/settings/artist-links", {"lastfm_user": value, "flickr_user": "changed"}, authenticated=True)
            self.assertEqual(response["status"], "400 Bad Request")
            self.assertEqual(app.get_app_setting(self.conn, "lastfm_user"), "listener")
            self.assertEqual(app.get_app_setting(self.conn, "flickr_user"), "12345@N00")
