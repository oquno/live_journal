import io
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlparse

import app


class HeadParser(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.in_head = False
        self.in_title = False
        self.title = ""
        self.meta = {}
        self.canonical = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "head":
            self.in_head = True
        if not self.in_head:
            return
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            self.meta[attrs.get("property") or attrs.get("name")] = attrs.get("content")
        elif tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs["href"]

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_head and self.in_title:
            self.title += data


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(app, "ADMIN_PASSWORD", "test-password"))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name, "journal.sqlite"))
        with sqlite3.connect(self.path) as conn:
            conn.executescript(Path(app.BASE_DIR, "schema.sql").read_text())
            conn.execute("INSERT INTO venues (id, name, slug) VALUES (1, ?, ?)",
                         ('会場 <&> "京都"', "京都"))
            conn.execute("INSERT INTO artists (id, name, slug) VALUES (1, ?, ?)",
                         ('演者 <&> "音楽"', "音楽"))
            conn.execute("INSERT INTO entries (id, event_date, title, venue_id, notes) VALUES (1, ?, ?, 1, ?)",
                         ("2026-10-16", 'ライブ <&> "秋"', "メモだけの秘密"))
            conn.execute("INSERT INTO entry_artists (entry_id, artist_id) VALUES (1, 1)")

    def request(self, path="/", query="", mode="public", authenticated=False, public_url="https://live.example.com", **extra):
        captured = {}

        def start_response(status, headers):
            captured.update(status=status, headers=dict(headers))

        environ = {"PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": "GET",
                   "HTTP_HOST": "internal.example:8000", "wsgi.url_scheme": "http",
                   "wsgi.input": io.BytesIO(b""), **extra}
        with patch.multiple(app, DB_PATH=self.path, APP_MODE=mode, PUBLIC_URL=public_url,
                            APP_TITLE='記録 <&> "音楽"', SESSION_SECRET="test-secret"):
            if authenticated:
                environ["HTTP_COOKIE"] = "live_journal_session=" + app.sign_session(app.ADMIN_USER)
            captured["body"] = b"".join(app.application(environ, start_response)).decode()
        captured["head"] = HeadParser(captured["body"])
        return captured

    def test_public_pages_have_text_metadata_without_images(self):
        for path in ("/", "/entries/1", "/artists/1", "/venues/1", "/stats"):
            with self.subTest(path=path):
                response = self.request(path)
                head = response["head"]
                self.assertEqual(response["status"], "200 OK")
                self.assertEqual(head.meta["og:title"], head.title)
                self.assertEqual(head.meta["twitter:title"], head.title)
                self.assertEqual(head.meta["og:site_name"], '記録 <&> "音楽"')
                self.assertEqual(head.meta["og:description"], head.meta["description"])
                self.assertEqual(head.meta["twitter:description"], head.meta["description"])
                self.assertEqual(head.meta["og:type"], "website")
                self.assertEqual(head.meta["og:locale"], "ja_JP")
                self.assertEqual(head.meta["twitter:card"], "summary")
                self.assertEqual(head.meta["og:url"], head.canonical)
                self.assertNotIn("og:image", head.meta)
                self.assertNotIn("twitter:image", head.meta)
                self.assertNotIn("robots", head.meta)

    def test_detail_escapes_names_once_and_uses_event_facts(self):
        response = self.request("/entries/1")
        head = response["head"]
        self.assertIn('ライブ <&> "秋"', head.title)
        self.assertIn('会場 <&> "京都"', head.meta["description"])
        self.assertIn('演者 <&> "音楽"', head.meta["description"])
        self.assertNotIn("メモだけの秘密", str(head.meta))
        self.assertNotIn("&amp;lt;", response["body"])
        self.assertEqual(head.canonical, "https://live.example.com/entries/1")

    def test_filters_and_pagination_have_canonical_queries(self):
        query = "artist=" + quote('演者 <&> "音楽"') + "&from=2026-01-01&utm_source=test&page=999"
        head = self.request(query=query)["head"]
        self.assertEqual(parse_qs(urlparse(head.canonical).query), {
            "artist": ['演者 <&> "音楽"'], "from": ["2026-01-01"],
        })
        self.assertIn("1件", head.meta["description"])
        with patch.object(app, "ENTRY_PAGE_SIZE", 1):
            with sqlite3.connect(self.path) as conn:
                conn.execute("INSERT INTO entries (event_date, venue_id) VALUES ('2026-10-17', 1)")
            self.assertEqual(self.request(query="page=2&utm_source=test")["head"].canonical,
                             "https://live.example.com/?page=2")

    def test_artist_and_venue_aliases_use_encoded_slugs(self):
        for kind, slug in (("artists", "音楽"), ("venues", "京都")):
            with self.subTest(kind=kind):
                alias = self.request(f"/{kind}/1")["head"]
                canonical = "https://live.example.com/" + kind + "/" + quote(slug, safe="")
                self.assertEqual(alias.canonical, canonical)
                self.assertEqual(self.request(f"/{kind}/{quote(slug)}")["head"].canonical, canonical)

    def test_stats_metadata_matches_selected_period_including_empty_period(self):
        self.assertEqual(self.request("/stats")["head"].canonical, "https://live.example.com/stats")
        for year, count in (("2026", 1), ("2025", 0)):
            with self.subTest(year=year):
                head = self.request("/stats", "year=" + year + "&utm_source=test")["head"]
                self.assertIn(year + "年", head.title)
                self.assertIn(f"ライブ記録 {count}件", head.meta["description"])
                self.assertEqual(head.canonical, "https://live.example.com/stats?year=" + year)
        head = self.request("/stats", "year=2025&from=2026-10-01&to=2026-10-31")["head"]
        self.assertEqual(head.canonical, "https://live.example.com/stats?from=2026-10-01&to=2026-10-31")
        self.assertIn("2026-10-01 〜 2026-10-31", head.title)

    def test_private_pages_require_login_and_never_expose_record_metadata(self):
        for path in ("/", "/entries/1", "/artists/1", "/venues/1", "/stats"):
            with self.subTest(path=path):
                response = self.request(path, mode="private")
                self.assertEqual(response["status"], "303 See Other")
                self.assertEqual(response["headers"]["Location"], "/login")
                head = self.request(path, mode="private", authenticated=True)["head"]
                self.assertEqual(head.meta["description"], app.APP_DESCRIPTION)
                self.assertEqual(head.meta["robots"], "noindex, nofollow")
                self.assertFalse(any(key.startswith(("og:", "twitter:")) for key in head.meta if key))
                self.assertIsNone(head.canonical)

    def test_login_management_and_error_pages_do_not_advertise_sharing(self):
        for path, query, status in (("/login", "", "200 OK"), ("/entries/new", "", "200 OK"),
                                    ("/entries/1/edit", "", "200 OK"), ("/settings/links", "", "200 OK"),
                                    ("/missing", "", "404 Not Found"), ("/stats", "year=invalid", "400 Bad Request")):
            with self.subTest(path=path):
                response = self.request(path, query, authenticated=True)
                self.assertEqual(response["status"], status)
                head = response["head"]
                self.assertEqual(head.meta["robots"], "noindex, nofollow")
                self.assertNotIn("og:title", head.meta)
                self.assertNotIn("twitter:card", head.meta)
                self.assertIsNone(head.canonical)

    def test_origin_uses_configuration_or_request_with_https_proxy(self):
        self.assertEqual(self.request()["head"].canonical, "https://live.example.com/")
        self.assertEqual(self.request(public_url="")["head"].canonical, "http://internal.example:8000/")
        self.assertEqual(self.request(public_url="", HTTP_HOST="live.example.com", HTTP_X_FORWARDED_PROTO="https")["head"].canonical,
                         "https://live.example.com/")
        self.assertEqual(self.request(public_url="", HTTP_HOST="live.example.com", **{"wsgi.url_scheme": "https"})["head"].canonical,
                         "https://live.example.com/")
        for host in ("live.example.com/path", "live.example.com\n", "[bad", "user@live.example.com", "live.example.com:bad"):
            with self.subTest(host=host):
                head = self.request(public_url="", HTTP_HOST=host)["head"]
                self.assertIsNone(head.canonical)
                self.assertNotIn("og:url", head.meta)

    def test_long_descriptions_are_bounded_and_whitespace_normalized(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE artists SET name = ? WHERE id = 1", ("演者\n   " + "長い名前" * 100,))
        description = self.request("/artists/1")["head"].meta["description"]
        self.assertLessEqual(len(description), 160)
        self.assertTrue(description.endswith("…"))
        self.assertNotIn("\n", description)
        self.assertNotIn("  ", description)


if __name__ == "__main__":
    unittest.main()
