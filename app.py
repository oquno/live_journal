import html
import os
import secrets
import sqlite3
from datetime import datetime
from http import cookies
from urllib.parse import parse_qs, quote, unquote, urlencode
from wsgiref.simple_server import make_server


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LIVE_JOURNAL_DB", os.path.join(BASE_DIR, "data", "live_journal.db"))
HOST = os.environ.get("LIVE_JOURNAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIVE_JOURNAL_PORT", "8000"))
APP_TITLE = os.environ.get("LIVE_JOURNAL_TITLE", "Live Journal")
APP_MODE = os.environ.get("LIVE_JOURNAL_MODE", "private")
ADMIN_USER = os.environ.get("LIVE_JOURNAL_USER", "admin")
ADMIN_PASSWORD = os.environ.get("LIVE_JOURNAL_PASSWORD", "admin")
ENTRY_PAGE_SIZE = 50
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
    import hashlib
    import hmac

    payload = username.encode("utf-8")
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{username}:{sig}"


def verify_session(token):
    import hashlib
    import hmac

    if ":" not in token:
        return None
    username, sig = token.split(":", 1)
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return username
    return None


def current_user(environ):
    jar = parse_cookies(environ)
    token = jar.get("live_journal_session")
    if not token:
        return None
    return verify_session(token.value)


def is_authenticated(environ):
    return current_user(environ) == ADMIN_USER


def can_view(environ):
    return APP_MODE == "public" or is_authenticated(environ)


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


def response_not_found(start_response):
    return response_html(start_response, layout("Not Found", "<h1>Not Found</h1>"), "404 Not Found")


def response_forbidden(start_response):
    return response_html(start_response, layout("Forbidden", "<h1>Forbidden</h1>"), "403 Forbidden")


def response_bad_request(start_response, message):
    return response_html(start_response, layout("Bad Request", f"<h1>Bad Request</h1><p>{esc(message)}</p>"), "400 Bad Request")


def nav(environ):
    auth = is_authenticated(environ)
    links = ['<a href="/">Entries</a>']
    if can_view(environ):
        links.append('<a href="/entries/new">New Entry</a>')
    if auth:
        links.append(
            '<form method="post" action="/logout" class="inline-form"><button type="submit">Logout</button></form>'
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


def render_entry_form(values, action, submit_label, errors=None):
    errors = errors or []
    artist_inputs = values.get("artists", [""])
    artist_seen_counts = values.get("artist_seen_counts", [""])
    purchase_names = values.get("purchase_names", [""])
    purchase_urls = values.get("purchase_urls", [""])
    purchase_notes = values.get("purchase_notes", [""])
    link_labels = values.get("link_labels", [""])
    link_urls = values.get("link_urls", [""])
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
    max_links = max(1, len(link_labels), len(link_urls))
    for idx in range(max_links):
        link_rows.append(
            f"""
            <div class="link-row">
              <input name="link_labels" value="{esc(link_labels[idx] if idx < len(link_labels) else '')}" placeholder="ラベル">
              <input type="url" name="link_urls" value="{esc(link_urls[idx] if idx < len(link_urls) else '')}" placeholder="URL">
            </div>
            """
        )
    link_block = "".join(link_rows)

    return f"""
    {error_block}
    <form method="post" action="{esc(action)}" class="entry-form">
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
        <input type="url" name="link_urls" value="" placeholder="URL">
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
        "link_urls": params.get("link_urls", [""]),
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
    link_count = max(len(values["link_labels"]), len(values["link_urls"]))
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
    link_urls = values["link_urls"]
    link_count = max(len(link_labels), len(link_urls))
    link_order = 1
    for idx in range(link_count):
        label = link_labels[idx].strip() if idx < len(link_labels) else ""
        link_url = link_urls[idx].strip() if idx < len(link_urls) else ""
        if not any([label, link_url]):
            continue
        conn.execute(
            """
            INSERT INTO entry_links(entry_id, label, url, display_order)
            VALUES (?, ?, ?, ?)
            """,
            (entry_id, label, link_url, link_order),
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
        "link_urls": [row["url"] for row in loaded["links"]] or [""],
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
    body = f"""
    <section class="single-column">
      <h1>Login</h1>
      <p class="muted">更新操作にはログインが必要です。</p>
      <form method="post" action="/login" class="entry-form compact">
        <label>ID
          <input type="text" name="username" value="{esc(ADMIN_USER)}">
        </label>
        <label>Password
          <input type="password" name="password">
        </label>
        <button type="submit">Login</button>
      </form>
    </section>
    """
    return response_html(start_response, layout("Login", body, environ, error))


def handle_login(environ, start_response):
    params = parse_body(environ)
    username = first(params, "username")
    password = first(params, "password")
    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        cookie = cookies.SimpleCookie()
        cookie["live_journal_session"] = sign_session(username)
        cookie["live_journal_session"]["path"] = "/"
        return redirect(start_response, "/", [("Set-Cookie", cookie.output(header="").strip())])
    return page_login(environ, start_response, "ログインに失敗しました。")


def handle_logout(start_response):
    cookie = cookies.SimpleCookie()
    cookie["live_journal_session"] = ""
    cookie["live_journal_session"]["path"] = "/"
    cookie["live_journal_session"]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    return redirect(start_response, "/login", [("Set-Cookie", cookie.output(header="").strip())])


def require_auth(environ, start_response):
    if not is_authenticated(environ):
        redirect(start_response, "/login")
        return False
    return True


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
        "link_urls": [""],
    }
    venue_options = "".join(f'<option value="{esc(row["name"])}">' for row in venues)
    body = f"""
    <section class="single-column">
      <h1>新規記録</h1>
      <datalist id="venues">{venue_options}</datalist>
      {render_entry_form(values, "/entries", "保存", errors)}
    </section>
    """
    return response_html(start_response, layout("New Entry", body, environ))


def handle_create_entry(environ, start_response):
    if not require_auth(environ, start_response):
        return [b""]
    values = collect_form_values(parse_body(environ))
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
          {'<a href="' + esc(row['lastfm_url']) + '" target="_blank" rel="noreferrer">Last.fm</a>' if row['lastfm_url'] else ''}
        </li>
        """
        for row in loaded["artists"]
    )
    purchases = "".join(
        f"<li>{esc(row['item_name'])} {'<a href=\"' + esc(row['item_url']) + '\" target=\"_blank\" rel=\"noreferrer\">link</a>' if row['item_url'] else ''} {esc(row['notes'])}</li>"
        for row in loaded["purchases"]
    ) or "<li>なし</li>"
    links = "".join(
        f'<li>{esc(row["label"])}: <a href="{esc(row["url"])}" target="_blank" rel="noreferrer">{esc(row["url"])}</a></li>'
        for row in loaded["links"]
    ) or "<li>なし</li>"
    actions = ""
    if is_authenticated(environ):
        actions = f"""
        <div class="actions">
          <a class="button-link" href="/entries/{entry['id']}/edit">編集</a>
          <form method="post" action="/entries/{entry['id']}/delete" class="inline-form" onsubmit="return confirm('削除しますか?');">
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
      {render_entry_form(entry_values, f"/entries/{entry_id}", "更新", errors)}
    </section>
    """
    return response_html(start_response, layout("Edit Entry", body, environ))


def handle_update_entry(environ, start_response, entry_id):
    if not require_auth(environ, start_response):
        return [b""]
    values = collect_form_values(parse_body(environ))
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
    full = os.path.join(BASE_DIR, path.lstrip("/"))
    if not os.path.isfile(full):
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
        return handle_logout(start_response)
    if path == "/" and method == "GET":
        return page_home(environ, start_response)
    if path == "/entries/new" and method == "GET":
        return page_new_entry(environ, start_response)
    if path == "/entries" and method == "POST":
        return handle_create_entry(environ, start_response)

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
