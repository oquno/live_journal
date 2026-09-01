import html
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import unicodedata
from datetime import datetime, timezone
from http import cookies
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from wsgiref.simple_server import make_server
import xml.etree.ElementTree as ET


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LIVE_JOURNAL_DB", os.path.join(BASE_DIR, "data", "live_journal.db"))
HOST = os.environ.get("LIVE_JOURNAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIVE_JOURNAL_PORT", "8000"))
APP_TITLE = os.environ.get("LIVE_JOURNAL_TITLE", "Live Journal")
APP_MODE = os.environ.get("LIVE_JOURNAL_MODE", "private")
ADMIN_USER = os.environ.get("LIVE_JOURNAL_USER", "admin")
ADMIN_PASSWORD = os.environ.get("LIVE_JOURNAL_PASSWORD")
ENTRY_PAGE_SIZE = 50
EXTERNAL_FETCH_TIMEOUT = 10
EXTERNAL_FETCH_MAX_BYTES = 2 * 1024 * 1024
LINK_CANDIDATE_DATE_WINDOW = 7
SESSION_SECRET_PATH = os.environ.get(
    "LIVE_JOURNAL_SESSION_SECRET_FILE",
    os.path.join(BASE_DIR, "data", "session_secret"),
)
SESSION_SECRET = None


def ensure_dirs():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(SESSION_SECRET_PATH), exist_ok=True)


def load_session_secret():
    env_secret = os.environ.get("LIVE_JOURNAL_SESSION_SECRET")
    if env_secret:
        return env_secret

    if os.path.exists(SESSION_SECRET_PATH):
        with open(SESSION_SECRET_PATH, "r", encoding="utf-8") as f:
            secret = f.read().strip()
        if secret:
            return secret

    secret = secrets.token_hex(32)
    with open(SESSION_SECRET_PATH, "w", encoding="utf-8") as f:
        f.write(secret + "\n")
    try:
        os.chmod(SESSION_SECRET_PATH, 0o600)
    except PermissionError:
        pass
    return secret


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    global SESSION_SECRET
    ensure_dirs()
    SESSION_SECRET = load_session_secret()
    conn = get_db()
    with open(os.path.join(BASE_DIR, "schema.sql"), "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(entry_artists)").fetchall()}
    if "seen_count_override" not in columns:
        conn.execute("ALTER TABLE entry_artists ADD COLUMN seen_count_override INTEGER")
    migrate_external_link_schema(conn)
    migrate_legacy_entry_links(conn)
    conn.commit()
    conn.close()


def migrate_legacy_entry_links(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(entries)").fetchall()}
    legacy_links = (("blog_url", "Blog", 1), ("flickr_url", "Flickr", 2))
    for column, label, display_order in legacy_links:
        if column not in columns:
            continue
        conn.execute(
            f"""
            INSERT INTO entry_links(entry_id, label, url, display_order)
            SELECT e.id, ?, TRIM(e.{column}), ?
            FROM entries e
            WHERE TRIM(COALESCE(e.{column}, '')) != ''
              AND NOT EXISTS (
                SELECT 1 FROM entry_links el
                WHERE el.entry_id = e.id AND el.url = TRIM(e.{column})
              )
            """,
            (label, display_order),
        )


def migrate_external_link_schema(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(entry_links)").fetchall()}
    for column, definition in (
        ("title", "TEXT"),
        ("source_candidate_id", "INTEGER"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE entry_links ADD COLUMN {column} {definition}")


def get_app_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_app_setting(conn, key, value):
    conn.execute(
        """
        INSERT INTO app_settings(key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """,
        (key, value),
    )


def delete_app_setting(conn, key):
    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))


def slugify(text):
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    parts = [part for part in base.split("-") if part]
    return "-".join(parts) or "item"


def ensure_unique_slug(conn, table, name):
    base = slugify(name)
    slug = base
    i = 2
    while True:
        row = conn.execute(f"SELECT id FROM {table} WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return slug
        slug = f"{base}-{i}"
        i += 1


def get_or_create_artist(conn, name):
    name = name.strip()
    row = conn.execute("SELECT id, slug FROM artists WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    slug = ensure_unique_slug(conn, "artists", name)
    cur = conn.execute(
        "INSERT INTO artists(name, slug, created_at, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (name, slug),
    )
    return cur.lastrowid


def get_or_create_venue(conn, name):
    name = name.strip()
    row = conn.execute("SELECT id, slug FROM venues WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    slug = ensure_unique_slug(conn, "venues", name)
    cur = conn.execute(
        "INSERT INTO venues(name, slug, created_at, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (name, slug),
    )
    return cur.lastrowid


def url_ok(value):
    return not value or value.startswith("http://") or value.startswith("https://")


def safe_external_url(value):
    value = (value or "").strip()
    return value if url_ok(value) else ""


def external_url_allowed(value):
    value = (value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except socket.gaierror:
        return False
    return all(
        not (
            ipaddress.ip_address(address).is_private
            or ipaddress.ip_address(address).is_loopback
            or ipaddress.ip_address(address).is_link_local
            or ipaddress.ip_address(address).is_multicast
            or ipaddress.ip_address(address).is_reserved
            or ipaddress.ip_address(address).is_unspecified
        )
        for address in addresses
    )


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        if not external_url_allowed(new_url):
            raise ValueError("外部 URL のリダイレクト先が許可されていません。")
        return super().redirect_request(request, file, code, message, headers, new_url)


SAFE_EXTERNAL_OPENER = build_opener(SafeRedirectHandler)


def fetch_external_bytes(url):
    if not external_url_allowed(url):
        raise ValueError("外部 URL は http/https の公開ホストである必要があります。")
    request = Request(url, headers={"User-Agent": "LiveJournal/1.0"})
    with SAFE_EXTERNAL_OPENER.open(request, timeout=EXTERNAL_FETCH_TIMEOUT) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > EXTERNAL_FETCH_MAX_BYTES:
            raise ValueError("取得するデータが大きすぎます。")
        chunks = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > EXTERNAL_FETCH_MAX_BYTES:
                raise ValueError("取得するデータが大きすぎます。")
            chunks.append(chunk)
    return b"".join(chunks)


def external_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).date().isoformat()
        except (TypeError, ValueError):
            return None


def xml_child_text(node, local_name):
    for child in list(node):
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return "".join(child.itertext()).strip()
    return ""


def parse_feed(data):
    root = ET.fromstring(data)
    nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in ("item", "entry")]
    candidates = []
    for node in nodes:
        title = xml_child_text(node, "title")
        link = ""
        external_id = ""
        published = ""
        for child in list(node):
            local_name = child.tag.rsplit("}", 1)[-1]
            if local_name == "link":
                candidate_link = child.attrib.get("href", "") or "".join(child.itertext()).strip()
                if not link or child.attrib.get("rel") == "alternate":
                    link = candidate_link
            elif local_name in ("guid", "id") and not external_id:
                external_id = "".join(child.itertext()).strip()
            elif local_name in ("pubDate", "published", "updated", "date") and not published:
                published = "".join(child.itertext()).strip()
        if not external_id:
            external_id = link
        if title and url_ok(link) and external_id:
            candidates.append(
                {"external_id": external_id, "title": title, "url": link, "published_at": external_date(published)}
            )
    return candidates


def flickr_api_call(method, params, api_key=None):
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("Flickr API key が設定されていません。")
    query = {"method": method, "api_key": api_key, "format": "json", "nojsoncallback": "1"}
    query.update(params)
    url = "https://api.flickr.com/services/rest/?" + urlencode(query)
    payload = json.loads(fetch_external_bytes(url).decode("utf-8"))
    if payload.get("stat") != "ok":
        message = payload.get("message") or "Flickr API の取得に失敗しました。"
        raise ValueError(message)
    return payload


def flickr_text(value):
    if isinstance(value, dict):
        return str(value.get("_content", ""))
    return str(value or "")


def flickr_candidates(source, api_key=None):
    user_id = (source["flickr_user_id"] or "").strip()
    account_url = (source["account_url"] or "").strip().rstrip("/")
    if not user_id:
        if not account_url:
            raise ValueError("Flickr アカウント URL またはユーザー ID が必要です。")
        user = flickr_api_call("flickr.urls.lookupUser", {"url": account_url}, api_key)
        user_id = user["user"]["id"]
    payload = flickr_api_call(
        "flickr.photosets.getList",
        {"user_id": user_id, "per_page": "500", "primary_photo_extras": "date_upload,date_taken"},
        api_key,
    )
    candidates = []
    for photoset in payload.get("photosets", {}).get("photoset", []):
        set_id = str(photoset.get("id", "")).strip()
        title = flickr_text(photoset.get("title", "")).strip()
        if not set_id or not title:
            continue
        url = f"{account_url}/sets/{set_id}" if account_url else f"https://www.flickr.com/photos/{user_id}/sets/{set_id}"
        candidates.append(
            {
                "external_id": set_id,
                "title": title,
                "url": url,
                "published_at": external_date(photoset.get("date_create") or photoset.get("date_update")),
            }
        )
    return candidates, user_id


def refresh_link_source(conn, source_id):
    source = conn.execute("SELECT * FROM link_sources WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        return False, "リンク元が見つかりません。"
    try:
        resolved_user_id = source["flickr_user_id"]
        if source["kind"] == "blog":
            candidates = parse_feed(fetch_external_bytes(source["feed_url"]))
        else:
            api_key = get_app_setting(conn, "flickr_api_key")
            candidates, resolved_user_id = flickr_candidates(source, api_key)
        for candidate in candidates:
            conn.execute(
                """
                INSERT INTO link_candidates(source_id, external_id, title, url, published_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_id, external_id) DO UPDATE SET
                  title = excluded.title,
                  url = excluded.url,
                  published_at = excluded.published_at,
                  fetched_at = CURRENT_TIMESTAMP
                """,
                (source_id, candidate["external_id"], candidate["title"], candidate["url"], candidate["published_at"]),
            )
        conn.execute(
            """
            UPDATE link_sources
            SET flickr_user_id = ?, last_fetched_at = CURRENT_TIMESTAMP, last_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (resolved_user_id, source_id),
        )
        conn.commit()
        return True, f"{len(candidates)} 件の候補を更新しました。"
    except (ET.ParseError, HTTPError, URLError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        conn.execute(
            "UPDATE link_sources SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (str(error), source_id),
        )
        conn.commit()
        return False, f"取得に失敗しました: {error}"


def normalize_match_text(value):
    value = unicodedata.normalize("NFKC", value or "")
    return " ".join(value.casefold().split())


def split_candidate_title(kind, title):
    title = (title or "").strip()
    date = None
    if kind == "flickr":
        match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(.+)$", title)
        if match:
            try:
                date = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date().isoformat()
                title = match.group(4).strip()
            except ValueError:
                pass
    if "@" not in title:
        return date, "", ""
    event_title, venue = title.rsplit("@", 1)
    return date, normalize_match_text(event_title), normalize_match_text(venue)


def link_candidate_matches(conn, event_date, event_title, venue):
    try:
        event_day = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        return []
    expected_title = normalize_match_text(event_title)
    expected_venue = normalize_match_text(venue)
    rows = conn.execute(
        """
        SELECT c.*, s.kind, s.name AS source_name
        FROM link_candidates c
        JOIN link_sources s ON s.id = c.source_id
        WHERE s.enabled = 1
        ORDER BY c.published_at DESC, c.id DESC
        """
    ).fetchall()
    matches = []
    for row in rows:
        flickr_date, candidate_title, candidate_venue = split_candidate_title(row["kind"], row["title"])
        pair_match = bool(expected_title and expected_venue and candidate_title == expected_title and candidate_venue == expected_venue)
        candidate_day = flickr_date or row["published_at"]
        distance = None
        if candidate_day:
            try:
                distance = abs((datetime.strptime(candidate_day, "%Y-%m-%d").date() - event_day).days)
            except ValueError:
                pass
        if not pair_match and (distance is None or distance > LINK_CANDIDATE_DATE_WINDOW):
            continue
        score = 100 if pair_match else 20
        if distance is not None:
            score += max(0, LINK_CANDIDATE_DATE_WINDOW - distance)
        matches.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "source_name": row["source_name"],
                "title": row["title"],
                "url": row["url"],
                "published_at": row["published_at"],
                "score": score,
                "exact": pair_match and (row["kind"] != "flickr" or flickr_date == event_date),
            }
        )
    return sorted(matches, key=lambda item: (-item["score"], item["title"]))[:100]


def parse_body(environ):
    length = int(environ.get("CONTENT_LENGTH") or 0)
    raw = environ["wsgi.input"].read(length).decode("utf-8")
    return parse_qs(raw, keep_blank_values=True)


def parse_query(environ):
    return parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)


def first(params, key, default=""):
    values = params.get(key)
    if not values:
        return default
    return values[0]


def all_values(params, key):
    return [value for value in params.get(key, []) if value.strip()]


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def url_path_segment(value):
    return quote("" if value is None else str(value), safe="")


def decode_path_segment(value):
    if value is None:
        return ""
    if "%" in value:
        return unquote(value)
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def positive_int(value, default=1):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def query_path(params, page):
    pairs = []
    for key in ("q", "artist", "venue", "from", "to"):
        value = first(params, key).strip()
        if value:
            pairs.append((key, value))
    if page > 1:
        pairs.append(("page", str(page)))
    query = urlencode(pairs)
    return "/" + (f"?{query}" if query else "")


def parse_cookies(environ):
    jar = cookies.SimpleCookie()
    jar.load(environ.get("HTTP_COOKIE", ""))
    return jar


def sign_session(username):
    payload = username.encode("utf-8")
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{username}:{sig}"


def verify_session(token):
    if ":" not in token:
        return None
    username, sig = token.split(":", 1)
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return username
    return None


def session_cookie_value(environ):
    jar = parse_cookies(environ)
    token = jar.get("live_journal_session")
    if not token:
        return ""
    return token.value


def current_user(environ):
    token = session_cookie_value(environ)
    if not token:
        return None
    return verify_session(token)


def is_authenticated(environ):
    return current_user(environ) == ADMIN_USER


def can_view(environ):
    return APP_MODE == "public" or is_authenticated(environ)


def csrf_token(environ):
    token = session_cookie_value(environ)
    if not token:
        return ""
    payload = f"csrf:{token}".encode("utf-8")
    return hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def csrf_input(environ):
    token = csrf_token(environ)
    if not token:
        return ""
    return f'<input type="hidden" name="csrf_token" value="{esc(token)}">'


def verify_csrf(environ, params):
    expected = csrf_token(environ)
    submitted = first(params, "csrf_token")
    return bool(expected) and hmac.compare_digest(expected, submitted)


def is_secure_request(environ):
    if environ.get("wsgi.url_scheme") == "https":
        return True
    forwarded_proto = environ.get("HTTP_X_FORWARDED_PROTO", "").split(",", 1)[0].strip()
    return forwarded_proto == "https"


def session_cookie(name, value, environ):
    cookie = cookies.SimpleCookie()
    cookie[name] = value
    cookie[name]["path"] = "/"
    cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "Lax"
    if is_secure_request(environ):
        cookie[name]["secure"] = True
    return cookie


def redirect(start_response, location, extra_headers=None):
    headers = [("Location", location)]
    if extra_headers:
        headers.extend(extra_headers)
    start_response("303 See Other", headers)
    return [b""]


def response_html(start_response, body, status="200 OK", headers=None):
    base_headers = [("Content-Type", "text/html; charset=utf-8")]
    if headers:
        base_headers.extend(headers)
    start_response(status, base_headers)
    return [body.encode("utf-8")]


def response_json(start_response, payload, status="200 OK"):
    start_response(status, [("Content-Type", "application/json; charset=utf-8")])
    return [json.dumps(payload, ensure_ascii=False).encode("utf-8")]


def response_not_found(start_response):
    return response_html(start_response, layout("Not Found", "<h1>Not Found</h1>"), "404 Not Found")


def response_forbidden(start_response):
    return response_html(start_response, layout("Forbidden", "<h1>Forbidden</h1>"), "403 Forbidden")


def response_bad_request(start_response, message):
    return response_html(start_response, layout("Bad Request", f"<h1>Bad Request</h1><p>{esc(message)}</p>"), "400 Bad Request")


def nav(environ):
    auth = is_authenticated(environ)
    links = ['<a href="/">Entries</a>']
    if auth:
        links.append('<a href="/entries/new">New Entry</a>')
        links.append('<a href="/settings/links">Link Sources</a>')
    if auth:
        links.append(
            f'<form method="post" action="/logout" class="inline-form">{csrf_input(environ)}<button type="submit">Logout</button></form>'
        )
    else:
        links.append('<a href="/login">Login</a>')
    return "".join(f"<li>{item}</li>" for item in links)


def flash_html(message):
    if not message:
        return ""
    return f'<div class="flash">{esc(message)}</div>'


def layout(title, content, environ=None, flash_message=""):
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - {esc(APP_TITLE)}</title>
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  <header class="site-header">
    <div class="wrap">
      <div class="brand"><a href="/">{esc(APP_TITLE)}</a></div>
      <nav><ul class="nav">{nav(environ) if environ else ''}</ul></nav>
    </div>
  </header>
  <main class="wrap">
    {flash_html(flash_message)}
    {content}
  </main>
</body>
</html>"""


def render_entry_card(row):
    artists = ""
    if row["artist_items"]:
        artist_links = []
        for item in row["artist_items"].split("||"):
            name, _, slug = item.partition("\t")
            if not name:
                continue
            artist_links.append(
                f'<a class="typed-link artist-link" href="/artists/{url_path_segment(slug)}">'
                f'<span aria-hidden="true">🎤</span>{esc(name)}</a>'
            )
        artists = " ".join(artist_links)
    summary = esc((row["notes"] or "")[:120])
    title = esc(row["title"] or "(untitled)")
    return f"""
<article class="card">
  <div class="card-head">
    <div>
      <h2><a class="typed-link event-link" href="/entries/{row['id']}">{title}</a></h2>
      <p class="card-meta">
        <span class="typed-link date-item"><span aria-hidden="true">📅</span>{esc(row['event_date'])}</span>
        <a class="typed-link venue-link" href="/venues/{url_path_segment(row['venue_slug'])}"><span aria-hidden="true">📍</span>{esc(row['venue_name'])}</a>
      </p>
    </div>
  </div>
  <p class="artist-links">{artists}</p>
  <p class="muted">{summary}</p>
</article>"""


def render_pagination(params, page, total_count, page_size):
    if total_count <= page_size:
        return ""
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    links = []
    if page > 1:
        links.append(f'<a href="{esc(query_path(params, page - 1))}">前へ</a>')
    for number in range(1, total_pages + 1):
        if number == page:
            links.append(f'<span aria-current="page">{number}</span>')
        else:
            links.append(f'<a href="{esc(query_path(params, number))}">{number}</a>')
    if page < total_pages:
        links.append(f'<a href="{esc(query_path(params, page + 1))}">次へ</a>')
    return f'<nav class="pagination" aria-label="ページネーション">{"".join(links)}</nav>'


def load_entry(conn, entry_id):
    row = conn.execute(
        """
        SELECT e.*, v.name AS venue_name, v.slug AS venue_slug
        FROM entries e
        JOIN venues v ON v.id = e.venue_id
        WHERE e.id = ?
        """,
        (entry_id,),
    ).fetchone()
    if row is None:
        return None
    artists = conn.execute(
        """
        SELECT a.id, a.name, a.slug, a.lastfm_url, ea.billing_order, ea.seen_count_override
        FROM entry_artists ea
        JOIN artists a ON a.id = ea.artist_id
        WHERE ea.entry_id = ?
        ORDER BY ea.billing_order, a.name
        """,
        (entry_id,),
    ).fetchall()
    artists = [dict(row) for row in artists]
    for artist in artists:
        artist["seen_count"] = compute_seen_count(conn, artist["id"], entry_id)
    purchases = conn.execute(
        "SELECT * FROM purchases WHERE entry_id = ? ORDER BY display_order, id",
        (entry_id,),
    ).fetchall()
    links = conn.execute(
        "SELECT * FROM entry_links WHERE entry_id = ? ORDER BY display_order, id",
        (entry_id,),
    ).fetchall()
    return {"entry": row, "artists": artists, "purchases": purchases, "links": links}


def render_entry_form(values, action, submit_label, environ, errors=None):
    errors = errors or []
    artist_inputs = values.get("artists", [""])
    artist_seen_counts = values.get("artist_seen_counts", [""])
    purchase_names = values.get("purchase_names", [""])
    purchase_urls = values.get("purchase_urls", [""])
    purchase_notes = values.get("purchase_notes", [""])
    link_labels = values.get("link_labels", [""])
    link_titles = values.get("link_titles", [""])
    link_urls = values.get("link_urls", [""])
    link_candidate_ids = values.get("link_candidate_ids", [""])
    error_block = ""
    if errors:
        items = "".join(f"<li>{esc(error)}</li>" for error in errors)
        error_block = f'<div class="flash error"><ul>{items}</ul></div>'

    artist_rows = []
    max_artist_rows = max(len(artist_inputs), len(artist_seen_counts))
    for idx in range(max_artist_rows):
        artist_rows.append(
            f"""
            <div class="artist-row">
              <input name="artists" value="{esc(artist_inputs[idx] if idx < len(artist_inputs) else '')}" placeholder="演者名">
              <input name="artist_seen_counts" value="{esc(artist_seen_counts[idx] if idx < len(artist_seen_counts) else '')}" placeholder="何回目か(任意)" inputmode="numeric">
            </div>
            """
        )
    artist_fields = "".join(artist_rows)

    purchase_rows = []
    max_rows = max(1, len(purchase_names), len(purchase_urls), len(purchase_notes))
    for idx in range(max_rows):
        purchase_rows.append(
            f"""
            <div class="purchase-row">
              <input name="purchase_names" value="{esc(purchase_names[idx] if idx < len(purchase_names) else '')}" placeholder="購入物名">
              <input name="purchase_urls" value="{esc(purchase_urls[idx] if idx < len(purchase_urls) else '')}" placeholder="URL">
              <input name="purchase_notes" value="{esc(purchase_notes[idx] if idx < len(purchase_notes) else '')}" placeholder="メモ">
            </div>
            """
        )
    purchase_block = "".join(purchase_rows)

    link_rows = []
    max_links = max(1, len(link_labels), len(link_titles), len(link_urls), len(link_candidate_ids))
    for idx in range(max_links):
        link_rows.append(
            f"""
            <div class="link-row">
              <input name="link_labels" value="{esc(link_labels[idx] if idx < len(link_labels) else '')}" placeholder="ラベル">
              <input name="link_titles" value="{esc(link_titles[idx] if idx < len(link_titles) else '')}" placeholder="タイトル（任意）">
              <input type="url" name="link_urls" value="{esc(link_urls[idx] if idx < len(link_urls) else '')}" placeholder="URL">
              <input type="hidden" name="link_candidate_ids" value="{esc(link_candidate_ids[idx] if idx < len(link_candidate_ids) else '')}">
            </div>
            """
        )
    link_block = "".join(link_rows)

    return f"""
    {error_block}
    <form method="post" action="{esc(action)}" class="entry-form">
      {csrf_input(environ)}
      <label>開催日
        <input type="date" name="event_date" value="{esc(values.get('event_date', ''))}" required>
      </label>
      <label>イベント名
        <input type="text" name="title" value="{esc(values.get('title', ''))}">
      </label>
      <label>会場
        <input type="text" name="venue" value="{esc(values.get('venue', ''))}" required list="venues">
      </label>
      <fieldset>
        <legend>関連リンク</legend>
        <div id="link-fields">
          {link_block}
        </div>
        <button type="button" class="secondary-button" data-add-link>関連リンクを追加</button>
        <button type="button" class="secondary-button" data-load-link-candidates>候補を読み込む</button>
        <div id="link-candidates" class="link-candidates" aria-live="polite"></div>
        <p class="hint">ブログは「イベント名@会場名」、Flickr は「yyyy/MM/dd イベント名@会場名」と一致する候補を優先します。</p>
      </fieldset>
      <fieldset>
        <legend>演者</legend>
        <div id="artist-fields">
          {artist_fields}
        </div>
        <button type="button" class="secondary-button" data-add-artist>演者を追加</button>
        <p class="hint">回数を空欄にすると自動計算します。数値を入れるとその回を基準に以後の回数もつながります。</p>
      </fieldset>
      <fieldset>
        <legend>購入物</legend>
        <div id="purchase-fields">
          {purchase_block}
        </div>
        <button type="button" class="secondary-button" data-add-purchase>購入物を追加</button>
        <p class="hint">URL がなくても記録できます。</p>
      </fieldset>
      <label>メモ
        <textarea name="notes" rows="10">{esc(values.get('notes', ''))}</textarea>
      </label>
      <button type="submit">{esc(submit_label)}</button>
    </form>
    <template id="artist-row-template">
      <div class="artist-row">
        <input name="artists" value="" placeholder="演者名">
        <input name="artist_seen_counts" value="" placeholder="何回目か(任意)" inputmode="numeric">
      </div>
    </template>
    <template id="purchase-row-template">
      <div class="purchase-row">
        <input name="purchase_names" value="" placeholder="購入物名">
        <input name="purchase_urls" value="" placeholder="URL">
        <input name="purchase_notes" value="" placeholder="メモ">
      </div>
    </template>
    <template id="link-row-template">
      <div class="link-row">
        <input name="link_labels" value="" placeholder="ラベル">
        <input name="link_titles" value="" placeholder="タイトル（任意）">
        <input type="url" name="link_urls" value="" placeholder="URL">
        <input type="hidden" name="link_candidate_ids" value="">
      </div>
    </template>
    <script>
      (() => {{
        const addRow = (buttonSelector, fieldsSelector, templateSelector) => {{
          const addButton = document.querySelector(buttonSelector);
          const fields = document.querySelector(fieldsSelector);
          const template = document.querySelector(templateSelector);
          if (!addButton || !fields || !template) return;
          addButton.addEventListener('click', () => {{
            fields.appendChild(template.content.firstElementChild.cloneNode(true));
          }});
        }};
        addRow('[data-add-artist]', '#artist-fields', '#artist-row-template');
        addRow('[data-add-purchase]', '#purchase-fields', '#purchase-row-template');
        addRow('[data-add-link]', '#link-fields', '#link-row-template');
        const candidateButton = document.querySelector('[data-load-link-candidates]');
        const candidateContainer = document.querySelector('#link-candidates');
        const eventDate = document.querySelector('[name="event_date"]');
        const eventTitle = document.querySelector('[name="title"]');
        const venue = document.querySelector('[name="venue"]');
        const linkFields = document.querySelector('#link-fields');
        const escapeHtml = (value) => String(value).replace(/[&<>\"']/g, (character) => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '\"': '&quot;', "'": '&#39;'}}[character]));
        const addCandidate = (candidate) => {{
          if ([...linkFields.querySelectorAll('[name="link_candidate_ids"]')].some((input) => input.value === String(candidate.id))) return;
          const row = document.querySelector('#link-row-template').content.firstElementChild.cloneNode(true);
          row.querySelector('[name="link_labels"]').value = candidate.kind === 'flickr' ? 'Flickr' : 'Blog';
          row.querySelector('[name="link_titles"]').value = candidate.title;
          row.querySelector('[name="link_urls"]').value = candidate.url;
          row.querySelector('[name="link_candidate_ids"]').value = candidate.id;
          linkFields.appendChild(row);
        }};
        if (candidateButton) candidateButton.addEventListener('click', async () => {{
          if (!eventDate.value) {{
            candidateContainer.textContent = '先に開催日を入力してください。';
            return;
          }}
          candidateContainer.textContent = '候補を読み込んでいます…';
          const query = new URLSearchParams({{event_date: eventDate.value, title: eventTitle.value, venue: venue.value}});
          try {{
            const response = await fetch('/api/link-candidates?' + query.toString());
            if (!response.ok) throw new Error('候補の取得に失敗しました。');
            const data = await response.json();
            if (!data.candidates.length) {{
              candidateContainer.textContent = '一致する候補がありません。各リンク元の「今すぐ更新」と手動リンクをお試しください。';
              return;
            }}
            candidateContainer.innerHTML = data.candidates.map((candidate) => `
              <label class="candidate-row">
                <input type="checkbox" data-candidate-id="${{candidate.id}}" ${{candidate.exact ? 'checked' : ''}}>
                <span><strong>${{escapeHtml(candidate.source_name)}}</strong> ${{candidate.exact ? '（自動一致）' : '（参考候補）'}}<br>${{escapeHtml(candidate.title)}}</span>
              </label>`).join('');
            candidateContainer.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {{
              checkbox.addEventListener('change', () => {{
                const candidate = data.candidates.find((item) => String(item.id) === checkbox.dataset.candidateId);
                if (checkbox.checked) addCandidate(candidate);
              }});
            }});
            data.candidates.filter((candidate) => candidate.exact).forEach((candidate) => addCandidate(candidate));
            candidateContainer.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {{
              if (checkbox.checked) checkbox.disabled = true;
            }});
          }} catch (error) {{
            candidateContainer.textContent = error.message;
          }}
        }});
      }})();
    </script>
    """


def collect_form_values(params):
    return {
        "event_date": first(params, "event_date"),
        "title": first(params, "title"),
        "venue": first(params, "venue"),
        "notes": first(params, "notes"),
        "artists": params.get("artists", [""]),
        "artist_seen_counts": params.get("artist_seen_counts", [""]),
        "purchase_names": params.get("purchase_names", [""]),
        "purchase_urls": params.get("purchase_urls", [""]),
        "purchase_notes": params.get("purchase_notes", [""]),
        "link_labels": params.get("link_labels", [""]),
        "link_titles": params.get("link_titles", [""]),
        "link_urls": params.get("link_urls", [""]),
        "link_candidate_ids": params.get("link_candidate_ids", [""]),
    }


def validate_entry_form(values):
    errors = []
    if not values["event_date"]:
        errors.append("開催日は必須です。")
    else:
        try:
            datetime.strptime(values["event_date"], "%Y-%m-%d")
        except ValueError:
            errors.append("開催日の形式が不正です。")
    if not values["venue"].strip():
        errors.append("会場は必須です。")
    artists = [name.strip() for name in values["artists"] if name.strip()]
    if not artists:
        errors.append("演者を 1 件以上入力してください。")
    if len(set(artists)) != len(artists):
        errors.append("同じ演者を重複登録できません。")
    for count in values.get("artist_seen_counts", []):
        count = count.strip()
        if not count:
            continue
        if not count.isdigit() or int(count) <= 0:
            errors.append("何回目かは 1 以上の整数で入力してください。")
            break
    link_count = max(len(values["link_labels"]), len(values.get("link_titles", [])), len(values["link_urls"]))
    for idx in range(link_count):
        label = values["link_labels"][idx].strip() if idx < len(values["link_labels"]) else ""
        link_url = values["link_urls"][idx].strip() if idx < len(values["link_urls"]) else ""
        if not any([label, link_url]):
            continue
        if not label:
            errors.append("関連リンクのラベルを入力してください。")
            break
        if not link_url:
            errors.append("関連リンク URL を入力してください。")
            break
        if not url_ok(link_url):
            errors.append("関連リンク URL は http/https の URL を入力してください。")
            break
    for url in values["purchase_urls"]:
        if url.strip() and not url_ok(url.strip()):
            errors.append("購入物 URL は http/https の URL を入力してください。")
            break
    return errors


def save_entry(conn, values, entry_id=None):
    venue_id = get_or_create_venue(conn, values["venue"])
    title = values["title"].strip()
    notes = values["notes"].strip()
    if entry_id is None:
        cur = conn.execute(
            """
            INSERT INTO entries(event_date, title, venue_id, notes, visibility, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'private', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (values["event_date"], title, venue_id, notes),
        )
        entry_id = cur.lastrowid
    else:
        conn.execute(
            """
            UPDATE entries
            SET event_date = ?, title = ?, venue_id = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (values["event_date"], title, venue_id, notes, entry_id),
        )
        conn.execute("DELETE FROM entry_artists WHERE entry_id = ?", (entry_id,))
        conn.execute("DELETE FROM purchases WHERE entry_id = ?", (entry_id,))
        conn.execute("DELETE FROM entry_links WHERE entry_id = ?", (entry_id,))

    artist_names = values["artists"]
    artist_seen_counts = values.get("artist_seen_counts", [])
    artist_rows = max(len(artist_names), len(artist_seen_counts))
    billing_order = 1
    for idx in range(artist_rows):
        artist_name = artist_names[idx].strip() if idx < len(artist_names) else ""
        seen_count_raw = artist_seen_counts[idx].strip() if idx < len(artist_seen_counts) else ""
        if not artist_name:
            continue
        artist_id = get_or_create_artist(conn, artist_name)
        seen_count_override = int(seen_count_raw) if seen_count_raw else None
        conn.execute(
            "INSERT INTO entry_artists(entry_id, artist_id, billing_order, seen_count_override) VALUES (?, ?, ?, ?)",
            (entry_id, artist_id, billing_order, seen_count_override),
        )
        billing_order += 1

    names = values["purchase_names"]
    urls = values["purchase_urls"]
    notes_list = values["purchase_notes"]
    count = max(len(names), len(urls), len(notes_list))
    order = 1
    for idx in range(count):
        name = names[idx].strip() if idx < len(names) else ""
        url = urls[idx].strip() if idx < len(urls) else ""
        note = notes_list[idx].strip() if idx < len(notes_list) else ""
        if not any([name, url, note]):
            continue
        conn.execute(
            """
            INSERT INTO purchases(entry_id, item_name, item_url, notes, display_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entry_id, name, url, note, order),
        )
        order += 1

    link_labels = values["link_labels"]
    link_titles = values.get("link_titles", [])
    link_urls = values["link_urls"]
    link_candidate_ids = values.get("link_candidate_ids", [])
    link_count = max(len(link_labels), len(link_titles), len(link_urls), len(link_candidate_ids))
    link_order = 1
    for idx in range(link_count):
        label = link_labels[idx].strip() if idx < len(link_labels) else ""
        link_title = link_titles[idx].strip() if idx < len(link_titles) else ""
        link_url = link_urls[idx].strip() if idx < len(link_urls) else ""
        candidate_id = link_candidate_ids[idx].strip() if idx < len(link_candidate_ids) else ""
        if not any([label, link_url]):
            continue
        conn.execute(
            """
            INSERT INTO entry_links(entry_id, label, url, title, source_candidate_id, display_order)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entry_id, label, link_url, link_title, int(candidate_id) if candidate_id.isdigit() else None, link_order),
        )
        link_order += 1

    conn.commit()
    return entry_id


def load_entry_form_values(conn, entry_id):
    loaded = load_entry(conn, entry_id)
    if loaded is None:
        return None
    entry = loaded["entry"]
    values = {
        "event_date": entry["event_date"],
        "title": entry["title"] or "",
        "venue": entry["venue_name"],
        "notes": entry["notes"] or "",
        "artists": [row["name"] for row in loaded["artists"]] or [""],
        "artist_seen_counts": [str(row["seen_count_override"] or "") for row in loaded["artists"]] or [""],
        "purchase_names": [row["item_name"] or "" for row in loaded["purchases"]] or [""],
        "purchase_urls": [row["item_url"] or "" for row in loaded["purchases"]] or [""],
        "purchase_notes": [row["notes"] or "" for row in loaded["purchases"]] or [""],
        "link_labels": [row["label"] for row in loaded["links"]] or [""],
        "link_titles": [row["title"] or "" for row in loaded["links"]] or [""],
        "link_urls": [row["url"] for row in loaded["links"]] or [""],
        "link_candidate_ids": [str(row["source_candidate_id"] or "") for row in loaded["links"]] or [""],
    }
    return values


def artist_timeline_rows(conn, artist_id):
    rows = conn.execute(
        """
        SELECT ea.entry_id, ea.seen_count_override, e.event_date, e.created_at, e.id
        FROM entry_artists ea
        JOIN entries e ON e.id = ea.entry_id
        WHERE ea.artist_id = ?
        ORDER BY e.event_date ASC, e.created_at ASC, e.id ASC
        """,
        (artist_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def compute_seen_count(conn, artist_id, entry_id):
    current = 0
    for row in artist_timeline_rows(conn, artist_id):
        override = row["seen_count_override"]
        current = int(override) if override else current + 1
        if row["entry_id"] == entry_id:
            return current
    return None


def entry_filter_parts(params):
    clauses = []
    values = []
    q = first(params, "q").strip()
    artist = first(params, "artist").strip()
    venue = first(params, "venue").strip()
    date_from = first(params, "from").strip()
    date_to = first(params, "to").strip()
    if q:
        like = f"%{q}%"
        clauses.append(
            """
            (
              e.title LIKE ?
              OR e.notes LIKE ?
              OR v.name LIKE ?
              OR EXISTS (
                SELECT 1 FROM entry_artists qea
                JOIN artists qa ON qa.id = qea.artist_id
                WHERE qea.entry_id = e.id AND qa.name LIKE ?
              )
              OR EXISTS (
                SELECT 1 FROM purchases qp
                WHERE qp.entry_id = e.id AND qp.item_name LIKE ?
              )
              OR EXISTS (
                SELECT 1 FROM entry_links ql
                WHERE ql.entry_id = e.id AND (ql.label LIKE ? OR ql.url LIKE ?)
              )
            )
            """
        )
        values.extend([like, like, like, like, like, like, like])
    if artist:
        clauses.append(
            "EXISTS (SELECT 1 FROM entry_artists fea JOIN artists fa ON fa.id = fea.artist_id WHERE fea.entry_id = e.id AND fa.name = ?)"
        )
        values.append(artist)
    if venue:
        clauses.append("v.name = ?")
        values.append(venue)
    if date_from:
        clauses.append("e.event_date >= ?")
        values.append(date_from)
    if date_to:
        clauses.append("e.event_date <= ?")
        values.append(date_to)
    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)
    return where, values


def count_entries(conn, params):
    where, values = entry_filter_parts(params)
    return conn.execute(
        f"""
        SELECT COUNT(*)
        FROM entries e
        JOIN venues v ON v.id = e.venue_id
        {where}
        """,
        values,
    ).fetchone()[0]


def list_entries(conn, params, limit=None, offset=0):
    where, values = entry_filter_parts(params)
    pagination = ""
    if limit is not None:
        pagination = "LIMIT ? OFFSET ?"
        values = values + [limit, offset]
    return conn.execute(
        f"""
        SELECT e.id, e.event_date, e.title, e.notes, e.venue_id, v.name AS venue_name, v.slug AS venue_slug,
               GROUP_CONCAT(a.name || CHAR(9) || a.slug, '||') AS artist_items
        FROM entries e
        JOIN venues v ON v.id = e.venue_id
        LEFT JOIN entry_artists ea ON ea.entry_id = e.id
        LEFT JOIN artists a ON a.id = ea.artist_id
        {where}
        GROUP BY e.id
        ORDER BY e.event_date DESC, e.created_at DESC, e.id DESC
        {pagination}
        """,
        values,
    ).fetchall()


def page_home(environ, start_response):
    if not can_view(environ):
        return redirect(start_response, "/login")
    conn = get_db()
    params = parse_query(environ)
    total_count = count_entries(conn, params)
    total_pages = max(1, (total_count + ENTRY_PAGE_SIZE - 1) // ENTRY_PAGE_SIZE)
    page = min(positive_int(first(params, "page"), 1), total_pages)
    offset = (page - 1) * ENTRY_PAGE_SIZE
    entries = list_entries(conn, params, ENTRY_PAGE_SIZE, offset)
    artists = conn.execute("SELECT name FROM artists ORDER BY name").fetchall()
    venues = conn.execute("SELECT name FROM venues ORDER BY name").fetchall()
    cards = "".join(render_entry_card(row) for row in entries) or '<p class="muted">まだ記録がありません。</p>'
    pagination = render_pagination(params, page, total_count, ENTRY_PAGE_SIZE)
    artist_options = "".join(f'<option value="{esc(row["name"])}">' for row in artists)
    venue_options = "".join(f'<option value="{esc(row["name"])}">' for row in venues)
    body = f"""
    <section class="hero">
      <h1>ライブ記録</h1>
      <p class="muted">見たライブを残して、演者ごとの履歴をたどれます。</p>
    </section>
    <form method="get" action="/" class="filters">
      <input type="search" name="q" placeholder="キーワード" value="{esc(first(params, 'q'))}">
      <input type="text" name="artist" placeholder="演者名" value="{esc(first(params, 'artist'))}" list="artists">
      <input type="text" name="venue" placeholder="会場名" value="{esc(first(params, 'venue'))}" list="venues">
      <input type="date" name="from" value="{esc(first(params, 'from'))}">
      <input type="date" name="to" value="{esc(first(params, 'to'))}">
      <button type="submit">検索</button>
    </form>
    <datalist id="artists">{artist_options}</datalist>
    <datalist id="venues">{venue_options}</datalist>
    <section class="cards">{cards}</section>
    {pagination}
    """
    conn.close()
    return response_html(start_response, layout("Entries", body, environ))


def page_login(environ, start_response, error=""):
    password_notice = ""
    disabled = ""
    if not ADMIN_PASSWORD:
        password_notice = '<p class="flash error">LIVE_JOURNAL_PASSWORD が未設定のためログインできません。</p>'
        disabled = " disabled"
    body = f"""
    <section class="single-column">
      <h1>Login</h1>
      <p class="muted">更新操作にはログインが必要です。</p>
      {password_notice}
      <form method="post" action="/login" class="entry-form compact">
        <label>ID
          <input type="text" name="username" value="{esc(ADMIN_USER)}">
        </label>
        <label>Password
          <input type="password" name="password">
        </label>
        <button type="submit"{disabled}>Login</button>
      </form>
    </section>
    """
    return response_html(start_response, layout("Login", body, environ, error))


def handle_login(environ, start_response):
    params = parse_body(environ)
    username = first(params, "username")
    password = first(params, "password")
    if ADMIN_PASSWORD and hmac.compare_digest(username, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASSWORD):
        cookie = session_cookie("live_journal_session", sign_session(username), environ)
        return redirect(start_response, "/", [("Set-Cookie", cookie.output(header="").strip())])
    return page_login(environ, start_response, "ログインに失敗しました。")


def handle_logout(environ, start_response):
    params = parse_body(environ)
    if is_authenticated(environ) and not verify_csrf(environ, params):
        return response_forbidden(start_response)
    cookie = session_cookie("live_journal_session", "", environ)
    cookie["live_journal_session"]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    return redirect(start_response, "/login", [("Set-Cookie", cookie.output(header="").strip())])


def require_auth(environ, start_response):
    if not is_authenticated(environ):
        redirect(start_response, "/login")
        return False
    return True


def link_source_values(params):
    return {
        "kind": first(params, "kind", "blog"),
        "name": first(params, "name"),
        "feed_url": first(params, "feed_url"),
        "account_url": first(params, "account_url"),
        "flickr_user_id": first(params, "flickr_user_id"),
        "enabled": "1" if first(params, "enabled") == "1" else "0",
    }


def validate_link_source(values):
    errors = []
    if values["kind"] not in ("blog", "flickr"):
        errors.append("リンク元の種類が不正です。")
    if not values["name"].strip():
        errors.append("表示名を入力してください。")
    if values["kind"] == "blog":
        if not values["feed_url"].strip():
            errors.append("ブログの RSS/Atom URL を入力してください。")
        elif not url_ok(values["feed_url"].strip()):
            errors.append("フィード URL は http/https の URL を入力してください。")
    if values["kind"] == "flickr":
        if values["account_url"].strip() and not url_ok(values["account_url"].strip()):
            errors.append("Flickr アカウント URL は http/https の URL を入力してください。")
        if not values["account_url"].strip() and not values["flickr_user_id"].strip():
            errors.append("Flickr アカウント URL またはユーザー ID を入力してください。")
    return errors


def render_link_source_fields(values):
    kind = values.get("kind", "blog")
    enabled = " checked" if values.get("enabled", "1") == "1" else ""
    return f"""
      <label>種類
        <select name="kind" data-source-kind-select>
          <option value="blog"{' selected' if kind == 'blog' else ''}>ブログ RSS/Atom</option>
          <option value="flickr"{' selected' if kind == 'flickr' else ''}>Flickr アルバム</option>
        </select>
      </label>
      <label>表示名
        <input name="name" value="{esc(values.get('name', ''))}" placeholder="個人ブログ">
      </label>
      <div class="source-field-group" data-source-kind-group="blog">
        <label>ブログの RSS/Atom URL
          <input type="url" name="feed_url" value="{esc(values.get('feed_url', ''))}" placeholder="https://example.com/feed">
        </label>
        <p class="hint">ブログ記事タイトルは「イベント名@会場名」の形式で照合します。</p>
      </div>
      <div class="source-field-group" data-source-kind-group="flickr">
        <label>Flickr アカウント URL
          <input type="url" name="account_url" value="{esc(values.get('account_url', ''))}" placeholder="https://www.flickr.com/photos/example/">
        </label>
        <label>Flickr ユーザー ID（NSID、任意）
          <input name="flickr_user_id" value="{esc(values.get('flickr_user_id', ''))}" placeholder="未入力なら API で解決">
        </label>
        <p class="hint">アルバムタイトルは「yyyy/MM/dd イベント名@会場名」の形式で照合します。</p>
      </div>
      <label class="checkbox-label"><input type="checkbox" name="enabled" value="1"{enabled}> 有効</label>
    """


def render_flickr_api_key_form(environ, configured):
    status = "設定済み" if configured else "未設定"
    return f"""
    <section class="source-card api-key-card">
      <h2>Flickr API key</h2>
      <p>現在の状態: <strong>{status}</strong>。この key はすべての Flickr リンク元で共有します。</p>
      <p class="external-help">API key は <a href="https://www.flickr.com/services/apps/" target="_blank" rel="noopener noreferrer">Flickr App Garden でアプリを作成して取得</a>します。</p>
      <ol class="help-list">
        <li>Flickr にログインして App Garden を開く</li>
        <li>アプリを作成し、表示された API key をコピーする</li>
        <li>下の欄に貼り付けて保存する</li>
      </ol>
      <p class="hint">入力欄は既存の key を表示しません。空欄で保存すると現在値を維持します。</p>
      <form method="post" action="/settings/flickr-api-key" class="entry-form">
        {csrf_input(environ)}
        <label>API key
          <input type="password" name="flickr_api_key" value="" autocomplete="new-password" placeholder="Flickr App Garden で取得した API key">
        </label>
        <label class="checkbox-label"><input type="checkbox" name="clear_flickr_api_key" value="1"> 保存済みの API key を削除する</label>
        <button type="submit">API key を保存</button>
      </form>
    </section>
    """


def page_link_settings(environ, start_response, values=None, errors=None):
    if not require_auth(environ, start_response):
        return [b""]
    conn = get_db()
    sources = conn.execute("SELECT * FROM link_sources ORDER BY kind, name, id").fetchall()
    flickr_key_configured = bool(get_app_setting(conn, "flickr_api_key"))
    conn.close()
    errors = errors or []
    error_block = ""
    if errors:
        error_block = '<div class="flash error"><ul>' + "".join(f"<li>{esc(error)}</li>" for error in errors) + "</ul></div>"
    source_blocks = []
    for source in sources:
        source_values = dict(source)
        source_values["enabled"] = "1" if source["enabled"] else "0"
        last_status = "未取得"
        if source["last_fetched_at"]:
            last_status = f"最終取得: {esc(source['last_fetched_at'])}"
        if source["last_error"]:
            last_status += f" / <span class=\"source-error\">{esc(source['last_error'])}</span>"
        source_blocks.append(
            f"""
            <section class="source-card">
              <form method="post" action="/settings/links/{source['id']}" class="entry-form">
                {csrf_input(environ)}
                {render_link_source_fields(source_values)}
                <div class="actions"><button type="submit">設定を更新</button></div>
              </form>
              <p class="source-status">{last_status}</p>
              <div class="actions">
                <form method="post" action="/settings/links/{source['id']}/refresh" class="inline-form">{csrf_input(environ)}<button type="submit" class="secondary-button">今すぐ更新</button></form>
                <form method="post" action="/settings/links/{source['id']}/delete" class="inline-form" onsubmit="return confirm('このリンク元を削除しますか?');">{csrf_input(environ)}<button type="submit" class="danger">削除</button></form>
              </div>
            </section>
            """
        )
    new_values = values or {"kind": "blog", "name": "", "feed_url": "", "account_url": "", "flickr_user_id": "", "enabled": "1"}
    query = parse_query(environ)
    message = first(query, "message")
    body = f"""
    <section class="single-column">
      <h1>リンク元設定</h1>
      <p class="muted">ブログや Flickr を登録してから「今すぐ更新」すると、エントリー作成時に候補を選べます。</p>
      {error_block}
      {render_flickr_api_key_form(environ, flickr_key_configured)}
      {''.join(source_blocks) or '<p class="muted">リンク元はまだ登録されていません。</p>'}
      <section class="source-card">
        <h2>リンク元を追加</h2>
        <form method="post" action="/settings/links" class="entry-form">
          {csrf_input(environ)}
          {render_link_source_fields(new_values)}
          <button type="submit">追加</button>
        </form>
      </section>
      <p class="hint">Flickr のアルバム取得には上の API key が必要です。ユーザー ID（NSID）が不明な場合はアカウント URL から解決します。</p>
    </section>
    <script>
      (() => {{
        const updateSourceFields = (select) => {{
          const form = select.closest('form');
          if (!form) return;
          form.querySelectorAll('[data-source-kind-group]').forEach((group) => {{
            group.hidden = group.dataset.sourceKindGroup !== select.value;
          }});
        }};
        document.querySelectorAll('[data-source-kind-select]').forEach((select) => {{
          updateSourceFields(select);
          select.addEventListener('change', () => updateSourceFields(select));
        }});
      }})();
    </script>
    """
    return response_html(start_response, layout("Link Sources", body, environ, message))


def handle_update_flickr_api_key(environ, start_response):
    if not require_auth(environ, start_response):
        return [b""]
    params = parse_body(environ)
    if not verify_csrf(environ, params):
        return response_forbidden(start_response)
    conn = get_db()
    if first(params, "clear_flickr_api_key") == "1":
        delete_app_setting(conn, "flickr_api_key")
        message = "Flickr API key を削除しました。"
    elif first(params, "flickr_api_key").strip():
        set_app_setting(conn, "flickr_api_key", first(params, "flickr_api_key").strip())
        message = "Flickr API key を保存しました。"
    else:
        message = "Flickr API key は変更していません。"
    conn.commit()
    conn.close()
    return redirect(start_response, "/settings/links?message=" + quote(message))


def save_link_source(conn, values, source_id=None):
    if source_id is None:
        cur = conn.execute(
            """
            INSERT INTO link_sources(kind, name, feed_url, account_url, flickr_user_id, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (values["kind"], values["name"].strip(), values["feed_url"].strip(), values["account_url"].strip(), values["flickr_user_id"].strip(), int(values["enabled"] == "1")),
        )
        source_id = cur.lastrowid
    else:
        conn.execute(
            "UPDATE entry_links SET source_candidate_id = NULL WHERE source_candidate_id IN (SELECT id FROM link_candidates WHERE source_id = ?)",
            (source_id,),
        )
        conn.execute("DELETE FROM link_candidates WHERE source_id = ?", (source_id,))
        conn.execute(
            """
            UPDATE link_sources
            SET kind = ?, name = ?, feed_url = ?, account_url = ?, flickr_user_id = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (values["kind"], values["name"].strip(), values["feed_url"].strip(), values["account_url"].strip(), values["flickr_user_id"].strip(), int(values["enabled"] == "1"), source_id),
        )
    conn.commit()
    return source_id


def handle_create_link_source(environ, start_response):
    if not require_auth(environ, start_response):
        return [b""]
    params = parse_body(environ)
    if not verify_csrf(environ, params):
        return response_forbidden(start_response)
    values = link_source_values(params)
    errors = validate_link_source(values)
    if errors:
        return page_link_settings(environ, start_response, values, errors)
    conn = get_db()
    save_link_source(conn, values)
    conn.close()
    return redirect(start_response, "/settings/links?message=" + quote("リンク元を追加しました。"))


def handle_update_link_source(environ, start_response, source_id):
    if not require_auth(environ, start_response):
        return [b""]
    params = parse_body(environ)
    if not verify_csrf(environ, params):
        return response_forbidden(start_response)
    values = link_source_values(params)
    errors = validate_link_source(values)
    if errors:
        return page_link_settings(environ, start_response, values, errors)
    conn = get_db()
    if conn.execute("SELECT id FROM link_sources WHERE id = ?", (source_id,)).fetchone() is None:
        conn.close()
        return response_not_found(start_response)
    save_link_source(conn, values, source_id)
    conn.close()
    return redirect(start_response, "/settings/links?message=" + quote("リンク元を更新しました。"))


def handle_refresh_link_source(environ, start_response, source_id):
    if not require_auth(environ, start_response):
        return [b""]
    params = parse_body(environ)
    if not verify_csrf(environ, params):
        return response_forbidden(start_response)
    conn = get_db()
    success, message = refresh_link_source(conn, source_id)
    conn.close()
    return redirect(start_response, "/settings/links?message=" + quote(message))


def handle_delete_link_source(environ, start_response, source_id):
    if not require_auth(environ, start_response):
        return [b""]
    params = parse_body(environ)
    if not verify_csrf(environ, params):
        return response_forbidden(start_response)
    conn = get_db()
    conn.execute("UPDATE entry_links SET source_candidate_id = NULL WHERE source_candidate_id IN (SELECT id FROM link_candidates WHERE source_id = ?)", (source_id,))
    conn.execute("DELETE FROM link_sources WHERE id = ?", (source_id,))
    conn.commit()
    conn.close()
    return redirect(start_response, "/settings/links?message=" + quote("リンク元を削除しました。"))


def handle_link_candidate_api(environ, start_response):
    if not is_authenticated(environ):
        return response_json(start_response, {"error": "認証が必要です。"}, "403 Forbidden")
    params = parse_query(environ)
    conn = get_db()
    candidates = link_candidate_matches(conn, first(params, "event_date"), first(params, "title"), first(params, "venue"))
    conn.close()
    return response_json(start_response, {"candidates": candidates})


def page_new_entry(environ, start_response, values=None, errors=None):
    if not require_auth(environ, start_response):
        return [b""]
    conn = get_db()
    venues = conn.execute("SELECT name FROM venues ORDER BY name").fetchall()
    conn.close()
    values = values or {
        "event_date": "",
        "title": "",
        "venue": "",
        "notes": "",
        "artists": [""],
        "artist_seen_counts": [""],
        "purchase_names": ["", "", ""],
        "purchase_urls": ["", "", ""],
        "purchase_notes": ["", "", ""],
        "link_labels": [""],
        "link_titles": [""],
        "link_urls": [""],
        "link_candidate_ids": [""],
    }
    venue_options = "".join(f'<option value="{esc(row["name"])}">' for row in venues)
    body = f"""
    <section class="single-column">
      <h1>新規記録</h1>
      <datalist id="venues">{venue_options}</datalist>
      {render_entry_form(values, "/entries", "保存", environ, errors)}
    </section>
    """
    return response_html(start_response, layout("New Entry", body, environ))


def handle_create_entry(environ, start_response):
    if not require_auth(environ, start_response):
        return [b""]
    params = parse_body(environ)
    if not verify_csrf(environ, params):
        return response_forbidden(start_response)
    values = collect_form_values(params)
    errors = validate_entry_form(values)
    if errors:
        return page_new_entry(environ, start_response, values, errors)
    conn = get_db()
    entry_id = save_entry(conn, values)
    conn.close()
    return redirect(start_response, f"/entries/{entry_id}")


def page_entry_detail(environ, start_response, entry_id):
    if not can_view(environ):
        return redirect(start_response, "/login")
    conn = get_db()
    loaded = load_entry(conn, entry_id)
    conn.close()
    if loaded is None:
        return response_not_found(start_response)
    entry = loaded["entry"]
    artists = "".join(
        f"""
        <li>
          <a class="typed-link artist-link" href="/artists/{url_path_segment(row['slug'])}"><span aria-hidden="true">🎤</span>{esc(row['name'])}</a>
          <span class="pill">{row['seen_count']}回目</span>
          {'<a href="' + esc(safe_external_url(row['lastfm_url'])) + '" target="_blank" rel="noreferrer">Last.fm</a>' if safe_external_url(row['lastfm_url']) else ''}
        </li>
        """
        for row in loaded["artists"]
    )
    purchases = "".join(
        f"<li>{esc(row['item_name'])} {'<a href=\"' + esc(safe_external_url(row['item_url'])) + '\" target=\"_blank\" rel=\"noreferrer\">link</a>' if safe_external_url(row['item_url']) else ''} {esc(row['notes'])}</li>"
        for row in loaded["purchases"]
    ) or "<li>なし</li>"
    links = "".join(
        f'<li>{esc(row["label"])}: <a href="{esc(safe_external_url(row["url"]))}" target="_blank" rel="noreferrer">{esc(row["title"] or safe_external_url(row["url"]))}</a></li>'
        for row in loaded["links"]
        if safe_external_url(row["url"])
    ) or "<li>なし</li>"
    actions = ""
    if is_authenticated(environ):
        actions = f"""
        <div class="actions">
          <a class="button-link" href="/entries/{entry['id']}/edit">編集</a>
          <form method="post" action="/entries/{entry['id']}/delete" class="inline-form" onsubmit="return confirm('削除しますか?');">
            {csrf_input(environ)}
            <button type="submit" class="danger">削除</button>
          </form>
        </div>
        """
    body = f"""
    <article class="single-column">
      <h1>{esc(entry['title'] or '(untitled)')}</h1>
      <p class="meta-line">
        <span class="typed-link date-item"><span aria-hidden="true">📅</span>{esc(entry['event_date'])}</span>
        <a class="typed-link venue-link" href="/venues/{url_path_segment(entry['venue_slug'])}"><span aria-hidden="true">📍</span>{esc(entry['venue_name'])}</a>
      </p>
      {actions}
      <section>
        <h2>演者</h2>
        <ul>{artists}</ul>
      </section>
      <section>
        <h2>購入物</h2>
        <ul>{purchases}</ul>
      </section>
      <section>
        <h2>リンク</h2>
        <ul>{links}</ul>
      </section>
      <section>
        <h2>メモ</h2>
        <pre class="note">{esc(entry['notes'] or '')}</pre>
      </section>
    </article>
    """
    return response_html(start_response,
        layout(f"{esc(entry['event_date'])} {esc(entry['title'] or '(untitled)')} at {esc(entry['venue_name'])}", body, environ))


def page_edit_entry(environ, start_response, entry_id, values=None, errors=None):
    if not require_auth(environ, start_response):
        return [b""]
    conn = get_db()
    entry_values = values or load_entry_form_values(conn, entry_id)
    conn.close()
    if entry_values is None:
        return response_not_found(start_response)
    body = f"""
    <section class="single-column">
      <h1>記録を編集</h1>
      {render_entry_form(entry_values, f"/entries/{entry_id}", "更新", environ, errors)}
    </section>
    """
    return response_html(start_response, layout("Edit Entry", body, environ))


def handle_update_entry(environ, start_response, entry_id):
    if not require_auth(environ, start_response):
        return [b""]
    params = parse_body(environ)
    if not verify_csrf(environ, params):
        return response_forbidden(start_response)
    values = collect_form_values(params)
    errors = validate_entry_form(values)
    if errors:
        return page_edit_entry(environ, start_response, entry_id, values, errors)
    conn = get_db()
    if load_entry(conn, entry_id) is None:
        conn.close()
        return response_not_found(start_response)
    save_entry(conn, values, entry_id)
    conn.close()
    return redirect(start_response, f"/entries/{entry_id}")


def handle_delete_entry(environ, start_response, entry_id):
    if not require_auth(environ, start_response):
        return [b""]
    params = parse_body(environ)
    if not verify_csrf(environ, params):
        return response_forbidden(start_response)
    conn = get_db()
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return redirect(start_response, "/")


def page_artist(environ, start_response, artist_ref):
    if not can_view(environ):
        return redirect(start_response, "/login")
    conn = get_db()
    if str(artist_ref).isdigit():
        artist = conn.execute("SELECT * FROM artists WHERE id = ?", (int(artist_ref),)).fetchone()
    else:
        artist = conn.execute("SELECT * FROM artists WHERE slug = ?", (artist_ref,)).fetchone()
    if artist is None:
        conn.close()
        return response_not_found(start_response)
    rows = conn.execute(
        """
        SELECT e.id, e.title, e.event_date, e.venue_id, v.name AS venue_name, v.slug AS venue_slug,
               ea.seen_count_override
        FROM entry_artists ea
        JOIN artists a ON a.id = ea.artist_id
        JOIN entries e ON e.id = ea.entry_id
        JOIN venues v ON v.id = e.venue_id
        WHERE a.id = ?
        ORDER BY e.event_date ASC, e.created_at ASC, e.id ASC
        """,
        (artist["id"],),
    ).fetchall()
    rows = [dict(row) for row in rows]
    for row in rows:
        row["seen_count"] = compute_seen_count(conn, artist["id"], row["id"])
    conn.close()
    items = "".join(
        f"""
        <li class="timeline-item">
          <span class="typed-link date-item"><span aria-hidden="true">📅</span>{esc(row['event_date'])}</span>
          <a class="typed-link event-link" href="/entries/{row['id']}">{esc(row['title'] or '(untitled)')}</a>
          <a class="typed-link venue-link" href="/venues/{url_path_segment(row['venue_slug'])}"><span aria-hidden="true">📍</span>{esc(row['venue_name'])}</a>
          <span class="pill">{row['seen_count']}回目</span>
        </li>
        """
        for row in rows
    ) or "<li>記録なし</li>"
    body = f"""
    <section class="single-column">
      <h1>{esc(artist['name'])}</h1>
      <p class="muted">観覧回数 {len(rows)} 回</p>
      <ul class="timeline-list">{items}</ul>
    </section>
    """
    return response_html(start_response, layout(artist["name"], body, environ))


def page_venue(environ, start_response, venue_ref):
    if not can_view(environ):
        return redirect(start_response, "/login")
    conn = get_db()
    if str(venue_ref).isdigit():
        venue = conn.execute("SELECT * FROM venues WHERE id = ?", (int(venue_ref),)).fetchone()
    else:
        venue = conn.execute("SELECT * FROM venues WHERE slug = ?", (venue_ref,)).fetchone()
    if venue is None:
        conn.close()
        return response_not_found(start_response)
    rows = conn.execute(
        """
        SELECT e.id, e.title, e.event_date, GROUP_CONCAT(a.name, ' / ') AS artists
        FROM entries e
        LEFT JOIN entry_artists ea ON ea.entry_id = e.id
        LEFT JOIN artists a ON a.id = ea.artist_id
        WHERE e.venue_id = ?
        GROUP BY e.id
        ORDER BY e.event_date DESC, e.created_at DESC, e.id DESC
        """,
        (venue["id"],),
    ).fetchall()
    conn.close()
    items = "".join(
        f"""
        <li class="timeline-item">
          <span class="typed-link date-item"><span aria-hidden="true">📅</span>{esc(row['event_date'])}</span>
          <a class="typed-link event-link" href="/entries/{row['id']}">{esc(row['title'] or '(untitled)')}</a>
          <span class="typed-link"><span aria-hidden="true">🎤</span>{esc(row['artists'] or '')}</span>
        </li>
        """
        for row in rows
    ) or "<li>記録なし</li>"
    body = f"""
    <section class="single-column">
      <h1>{esc(venue['name'])}</h1>
      <p class="muted">開催記録 {len(rows)} 件</p>
      <ul class="timeline-list">{items}</ul>
    </section>
    """
    return response_html(start_response, layout(venue["name"], body, environ))


def serve_static(start_response, path):
    static_root = os.path.realpath(os.path.join(BASE_DIR, "static"))
    relative = path[len("/static/"):]
    full = os.path.realpath(os.path.join(static_root, relative))
    if not full.startswith(static_root + os.sep) or not os.path.isfile(full):
        return response_not_found(start_response)
    content_type = "text/css; charset=utf-8" if full.endswith(".css") else "application/octet-stream"
    with open(full, "rb") as f:
        data = f.read()
    start_response("200 OK", [("Content-Type", content_type)])
    return [data]


def application(environ, start_response):
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "GET")

    if path.startswith("/static/"):
        return serve_static(start_response, path)

    if path == "/login" and method == "GET":
        return page_login(environ, start_response)
    if path == "/login" and method == "POST":
        return handle_login(environ, start_response)
    if path == "/logout" and method == "POST":
        return handle_logout(environ, start_response)
    if path == "/" and method == "GET":
        return page_home(environ, start_response)
    if path == "/entries/new" and method == "GET":
        return page_new_entry(environ, start_response)
    if path == "/entries" and method == "POST":
        return handle_create_entry(environ, start_response)
    if path == "/settings/links" and method == "GET":
        return page_link_settings(environ, start_response)
    if path == "/settings/links" and method == "POST":
        return handle_create_link_source(environ, start_response)
    if path == "/settings/flickr-api-key" and method == "POST":
        return handle_update_flickr_api_key(environ, start_response)
    if path == "/api/link-candidates" and method == "GET":
        return handle_link_candidate_api(environ, start_response)

    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] == "entries" and method == "GET":
        try:
            return page_entry_detail(environ, start_response, int(parts[1]))
        except ValueError:
            return response_not_found(start_response)
    if len(parts) == 3 and parts[0] == "entries" and parts[2] == "edit" and method == "GET":
        try:
            return page_edit_entry(environ, start_response, int(parts[1]))
        except ValueError:
            return response_not_found(start_response)
    if len(parts) == 2 and parts[0] == "entries" and method == "POST":
        try:
            return handle_update_entry(environ, start_response, int(parts[1]))
        except ValueError:
            return response_not_found(start_response)
    if len(parts) == 3 and parts[0] == "entries" and parts[2] == "delete" and method == "POST":
        try:
            return handle_delete_entry(environ, start_response, int(parts[1]))
        except ValueError:
            return response_not_found(start_response)
    if len(parts) == 3 and parts[0] == "settings" and parts[1] == "links" and method == "POST":
        try:
            return handle_update_link_source(environ, start_response, int(parts[2]))
        except ValueError:
            return response_not_found(start_response)
    if len(parts) == 4 and parts[0] == "settings" and parts[1] == "links" and parts[3] == "refresh" and method == "POST":
        try:
            return handle_refresh_link_source(environ, start_response, int(parts[2]))
        except ValueError:
            return response_not_found(start_response)
    if len(parts) == 4 and parts[0] == "settings" and parts[1] == "links" and parts[3] == "delete" and method == "POST":
        try:
            return handle_delete_link_source(environ, start_response, int(parts[2]))
        except ValueError:
            return response_not_found(start_response)
    if len(parts) == 2 and parts[0] == "artists" and method == "GET":
        return page_artist(environ, start_response, decode_path_segment(parts[1]))
    if len(parts) == 2 and parts[0] == "venues" and method == "GET":
        return page_venue(environ, start_response, decode_path_segment(parts[1]))

    return response_not_found(start_response)


if __name__ == "__main__":
    init_db()
    print(f"Serving on http://{HOST}:{PORT}")
    with make_server(HOST, PORT, application) as server:
        server.serve_forever()
