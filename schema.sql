CREATE TABLE IF NOT EXISTS venues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  lastfm_url TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_date TEXT NOT NULL,
  title TEXT,
  venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE RESTRICT,
  notes TEXT,
  blog_url TEXT,
  flickr_url TEXT,
  visibility TEXT NOT NULL DEFAULT 'private',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entry_artists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE RESTRICT,
  billing_order INTEGER NOT NULL DEFAULT 1,
  seen_count_override INTEGER,
  UNIQUE(entry_id, artist_id)
);

CREATE TABLE IF NOT EXISTS purchases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  item_name TEXT,
  item_url TEXT,
  notes TEXT,
  display_order INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_entries_event_date ON entries(event_date DESC);
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);
CREATE INDEX IF NOT EXISTS idx_venues_name ON venues(name);
CREATE INDEX IF NOT EXISTS idx_entry_artists_artist_entry ON entry_artists(artist_id, entry_id);
CREATE INDEX IF NOT EXISTS idx_purchases_entry_display ON purchases(entry_id, display_order);
