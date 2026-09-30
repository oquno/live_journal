import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import app
from stats import load_stats, stats_period


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "stats.sqlite")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(Path(app.BASE_DIR, "schema.sql").read_text())
        self.conn.executemany("INSERT INTO venues (id, name, slug) VALUES (?, ?, ?)", [
            (1, "京都の会場", "kyoto"), (2, "大阪の会場", "osaka"), (3, "未訪問", "unused"),
        ])
        self.conn.executemany("INSERT INTO artists (id, name, slug) VALUES (?, ?, ?)", [
            (1, "以前から見た演者", "a"), (2, "今年の演者 <&>", "b"),
            (3, "同日初観覧", "c"), (4, "未観覧", "unused"),
        ])
        self.conn.executemany("INSERT INTO entries (id, event_date, venue_id) VALUES (?, ?, ?)", [
            (1, "2025-12-31", 1), (2, "2026-01-01", 1), (3, "2026-01-01", 2),
            (4, "2026-03-01", 1), (5, "2027-01-01", 2),
        ])
        self.conn.executemany(
            "INSERT INTO entry_artists (entry_id, artist_id, seen_count_override) VALUES (?, ?, ?)",
            [(1, 1, 99), (2, 1, None), (2, 2, None), (2, 3, None),
             (3, 3, None), (4, 1, None), (4, 2, None), (5, 2, None)],
        )
        # Purchases and links must never multiply entry or artist totals.
        self.conn.executemany("INSERT INTO purchases (entry_id, item_name) VALUES (2, ?)", [("CD",), ("Tシャツ",)])
        self.conn.executemany("INSERT INTO entry_links (entry_id, label, url) VALUES (2, ?, ?)",
                              [("ブログ", "https://example.com/blog"), ("写真", "https://example.com/photos")])
        self.conn.commit()

    def request(self, query="", mode="public", cookie="", path="/stats"):
        captured = {}

        def start_response(status, headers):
            captured.update(status=status, headers=dict(headers))

        environ = {"PATH_INFO": path, "REQUEST_METHOD": "GET", "QUERY_STRING": query,
                   "HTTP_COOKIE": cookie, "wsgi.input": io.BytesIO(b"")}
        with patch.object(app, "DB_PATH", self.path), patch.object(app, "APP_MODE", mode), \
                patch.object(app, "SESSION_SECRET", "test-secret"):
            captured["body"] = b"".join(app.application(environ, start_response)).decode()
        return captured

    def test_year_counts_without_join_multiplication_or_overrides(self):
        data = load_stats(self.conn, stats_period("2026"))
        self.assertEqual((data["entries"], data["days"], data["appearances"]), (3, 2, 6))
        self.assertEqual(len(data["artists"]), 3)
        self.assertEqual(len(data["venues"]), 2)
        self.assertEqual([row["count"] for row in data["artists"]], [2, 2, 2])
        self.assertEqual([row["count"] for row in data["venues"]], [2, 1])
        self.assertEqual((data["new_artists"], data["known_artists"], data["repeat_artists"]), (2, 1, 3))
        self.assertEqual(data["grain"], "months")
        self.assertEqual(len(data["buckets"]), 12)
        self.assertEqual([row["count"] for row in data["buckets"][:3]], [2, 0, 1])
        self.assertEqual(sum(row["count"] for row in data["weekdays"]), 3)
        self.assertEqual(sum(row["count"] for row in data["seasons"]), 3)
        self.assertEqual(data["peak_month"], {"key": "2026-01", "count": 2})

    def test_date_boundaries_and_first_seen_across_all_records(self):
        data = load_stats(self.conn, stats_period(date_from="2026-01-01", date_to="2026-01-01"))
        self.assertEqual((data["entries"], data["days"], data["appearances"]), (2, 1, 4))
        self.assertEqual((data["new_artists"], data["known_artists"]), (2, 1))
        self.assertEqual(data["repeat_artists"], 1)
        later = load_stats(self.conn, stats_period(date_from="2026-03-01"))
        self.assertEqual((later["new_artists"], later["known_artists"]), (0, 2))
        earlier = load_stats(self.conn, stats_period(date_to="2025-12-31"))
        self.assertEqual((earlier["new_artists"], earlier["known_artists"]), (1, 0))

    def test_all_time_uses_annual_buckets_and_annual_artist_counts(self):
        self.conn.execute("UPDATE entries SET event_date = '2025-01-01' WHERE id = 1")
        data = load_stats(self.conn, stats_period())
        self.assertEqual(data["grain"], "years")
        self.assertEqual([row["count"] for row in data["buckets"]], [1, 3, 1])
        self.assertEqual(data["years"], ["2027", "2026", "2025"])
        self.assertEqual([row["year"] for row in data["annual"]], ["2027", "2026", "2025"])
        self.assertEqual(data["annual"][1]["artist_count"], 3)
        self.assertEqual([row["count"] for row in data["annual"][1]["top"]], [2, 2, 2])
        self.assertEqual((data["new_artists"], data["known_artists"]), (3, 0))

    def test_empty_period_and_empty_database(self):
        data = load_stats(self.conn, stats_period("2024"))
        self.assertEqual(data["entries"], 0)
        self.assertEqual(data["buckets"], [])
        self.assertEqual(data["new_artists"], 0)
        self.assertIn("この期間の記録がありません", self.request("year=2024")["body"])
        self.conn.execute("DELETE FROM entry_artists")
        self.conn.execute("DELETE FROM purchases")
        self.conn.execute("DELETE FROM entry_links")
        self.conn.execute("DELETE FROM entries")
        self.conn.commit()
        self.assertIn("ライブの記録から、足跡が見えてきます", self.request()["body"])

    def test_invalid_periods_return_bad_request(self):
        for query in ("year=0", "year=10000", "year=abcd", "from=2026-02-30",
                      "from=20260101", "from=2026-W01-1", "from=2026-03-01&to=2026-02-01"):
            with self.subTest(query=query):
                self.assertEqual(self.request(query)["status"], "400 Bad Request")
        self.assertEqual(stats_period("2025", "2026-01-01", "2026-02-01")["year"], "")

    def test_authentication_matches_existing_view_permissions(self):
        self.assertEqual(self.request(mode="public")["status"], "200 OK")
        private = self.request(mode="private")
        self.assertEqual(private["status"], "303 See Other")
        self.assertEqual(private["headers"]["Location"], "/login")
        with patch.object(app, "SESSION_SECRET", "test-secret"):
            token = app.sign_session(app.ADMIN_USER)
        private = self.request(mode="private", cookie=f"live_journal_session={token}")
        self.assertEqual(private["status"], "200 OK")

    def test_rendering_escapes_names_and_ranking_links_keep_period(self):
        response = self.request("year=2026")
        self.assertEqual(response["status"], "200 OK")
        self.assertIn("今年の演者 &lt;&amp;&gt;", response["body"])
        self.assertNotIn("今年の演者 <&>", response["body"])
        self.assertIn('aria-current="page"', response["body"])
        url = app.stats_entries_url(stats_period("2026"), artist="今年の演者 <&>")
        self.assertEqual(parse_qs(urlparse(url).query), {
            "from": ["2026-01-01"], "to": ["2026-12-31"], "artist": ["今年の演者 <&>"],
        })

    def test_drilldown_keeps_partial_months_and_leap_days(self):
        partial = stats_period(date_from="2026-01-15", date_to="2026-03-10")
        self.assertEqual(app.stats_bucket_period("2026-01", partial)["from"], "2026-01-15")
        self.assertEqual(app.stats_bucket_period("2026-03", partial)["to"], "2026-03-10")
        self.assertEqual(app.stats_bucket_period("2024-02", stats_period())["to"], "2024-02-29")
        self.assertEqual(app.stats_period_url(app.stats_bucket_period("2026", stats_period())), "/stats?year=2026")

    def test_monthly_granularity_limit_and_empty_months(self):
        short = load_stats(self.conn, stats_period(date_from="2025-01-01", date_to="2026-12-31"))
        self.assertEqual(short["grain"], "months")
        self.assertEqual(len(short["buckets"]), 24)
        self.assertEqual(short["buckets"][0]["count"], 0)
        longer = load_stats(self.conn, stats_period(date_from="2025-01-01", date_to="2027-01-31"))
        self.assertEqual(longer["grain"], "years")


if __name__ == "__main__":
    unittest.main()
