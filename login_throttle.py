"""SQLite-backed limits shared by every WSGI worker and process restart."""

import hashlib
import math
import time

WINDOW_SECONDS = 300
MAX_ATTEMPTS = 10


def reserve_attempt(conn, address, now=None):
    now = time.time() if now is None else now
    key = hashlib.sha256((address or "unknown").encode()).hexdigest()
    # Reserve before checking credentials so simultaneous workers cannot all pass.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM login_attempts WHERE window_start <= ?", (now - WINDOW_SECONDS,))
        row = conn.execute("SELECT window_start, attempts FROM login_attempts WHERE client = ?", (key,)).fetchone()
        if row and row[1] >= MAX_ATTEMPTS:
            return max(1, math.ceil(row[0] + WINDOW_SECONDS - now))
        conn.execute("""INSERT INTO login_attempts(client, window_start, attempts) VALUES (?, ?, 1)
                        ON CONFLICT(client) DO UPDATE SET attempts = attempts + 1""", (key, now))
    return 0
