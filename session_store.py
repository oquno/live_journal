"""Expiring, revocable sessions shared by all workers through SQLite."""
import hashlib
import hmac
import json
import re
import secrets
import time

SESSION_SECONDS = 12 * 60 * 60


def _synchronize(conn, username, password, secret):
    fingerprint = hmac.new(secret.encode(), json.dumps([username, password]).encode(), hashlib.sha256).hexdigest()
    conn.execute('BEGIN IMMEDIATE')
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'session_credentials'").fetchone()
    if row is None or not hmac.compare_digest(row[0], fingerprint):
        conn.execute('DELETE FROM auth_sessions')
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('session_credentials', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (fingerprint,))
    return bool(password)


def issue(conn, username, password, secret, now=None):
    now = time.time() if now is None else now
    with conn:
        if not _synchronize(conn, username, password, secret):
            raise ValueError('Administrator password is not configured')
        conn.execute('DELETE FROM auth_sessions WHERE expires_at <= ?', (now,))
        token = secrets.token_urlsafe(32)
        conn.execute('INSERT INTO auth_sessions(token_hash, expires_at) VALUES (?, ?)',
                     (hashlib.sha256(token.encode()).hexdigest(), now + SESSION_SECONDS))
    return token


def verify(conn, token, username, password, secret, now=None):
    now = time.time() if now is None else now
    with conn:
        enabled = _synchronize(conn, username, password, secret)
        if not enabled or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        row = conn.execute('SELECT expires_at FROM auth_sessions WHERE token_hash = ?',
                           (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return username if row and row[0] > now else None


def revoke(conn, token):
    with conn:
        conn.execute('DELETE FROM auth_sessions WHERE token_hash = ?',
                     (hashlib.sha256(token.encode()).hexdigest(),))
