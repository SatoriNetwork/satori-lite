"""Local SQLite storage for network datastream subscriptions."""

import os
import sqlite3
import threading
import time


class NetworkDB:
    """Thread-safe SQLite database for tracking subscribed datastreams."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_schema(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name TEXT NOT NULL,
                relay_url TEXT NOT NULL,
                provider_pubkey TEXT NOT NULL,
                name TEXT,
                description TEXT,
                cadence_seconds INTEGER,
                price_per_obs INTEGER DEFAULT 0,
                encrypted INTEGER DEFAULT 0,
                tags TEXT,
                active INTEGER DEFAULT 1,
                subscribed_at INTEGER NOT NULL,
                unsubscribed_at INTEGER,
                stale_since INTEGER,
                UNIQUE(stream_name, provider_pubkey)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name TEXT NOT NULL,
                provider_pubkey TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                value TEXT,
                event_id TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_obs_stream
            ON observations(stream_name, provider_pubkey, received_at DESC)
        """)
        # Migration: add stale_since if missing (existing DBs)
        try:
            conn.execute("SELECT stale_since FROM subscriptions LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN stale_since INTEGER")
        conn.commit()

    # ── Subscriptions ──────────────────────────────────────────────

    def subscribe(self, stream: dict, relay_url: str) -> int:
        """Subscribe to a stream. Returns row id."""
        conn = self._get_conn()
        tags = ','.join(stream.get('tags', []))
        conn.execute("""
            INSERT INTO subscriptions
                (stream_name, relay_url, provider_pubkey, name, description,
                 cadence_seconds, price_per_obs, encrypted, tags, active,
                 subscribed_at, stale_since)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL)
            ON CONFLICT(stream_name, provider_pubkey) DO UPDATE SET
                active = 1,
                relay_url = excluded.relay_url,
                name = excluded.name,
                description = excluded.description,
                cadence_seconds = excluded.cadence_seconds,
                price_per_obs = excluded.price_per_obs,
                encrypted = excluded.encrypted,
                tags = excluded.tags,
                subscribed_at = excluded.subscribed_at,
                unsubscribed_at = NULL,
                stale_since = NULL
        """, (
            stream['stream_name'],
            relay_url,
            stream['nostr_pubkey'],
            stream.get('name', ''),
            stream.get('description', ''),
            stream.get('cadence_seconds'),
            stream.get('price_per_obs', 0),
            1 if stream.get('encrypted') else 0,
            tags,
            int(time.time()),
        ))
        conn.commit()
        return conn.execute(
            "SELECT id FROM subscriptions WHERE stream_name=? AND provider_pubkey=?",
            (stream['stream_name'], stream['nostr_pubkey'])
        ).fetchone()[0]

    def unsubscribe(self, stream_name: str, provider_pubkey: str):
        """Soft-delete a subscription."""
        conn = self._get_conn()
        conn.execute("""
            UPDATE subscriptions SET active = 0, unsubscribed_at = ?
            WHERE stream_name = ? AND provider_pubkey = ?
        """, (int(time.time()), stream_name, provider_pubkey))
        conn.commit()

    def get_active(self) -> list[dict]:
        """Return all active subscriptions."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE active = 1 ORDER BY subscribed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def is_subscribed(self, stream_name: str, provider_pubkey: str) -> bool:
        """Check if actively subscribed to a stream."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT active FROM subscriptions WHERE stream_name=? AND provider_pubkey=?",
            (stream_name, provider_pubkey)
        ).fetchone()
        return row is not None and row['active'] == 1

    def mark_stale(self, stream_name: str, provider_pubkey: str):
        """Mark a subscription as stale (provider not delivering)."""
        conn = self._get_conn()
        conn.execute("""
            UPDATE subscriptions SET stale_since = ?
            WHERE stream_name = ? AND provider_pubkey = ? AND active = 1
        """, (int(time.time()), stream_name, provider_pubkey))
        conn.commit()

    def clear_stale(self, stream_name: str, provider_pubkey: str):
        """Clear stale status (found active source)."""
        conn = self._get_conn()
        conn.execute("""
            UPDATE subscriptions SET stale_since = NULL
            WHERE stream_name = ? AND provider_pubkey = ?
        """, (stream_name, provider_pubkey))
        conn.commit()

    def update_relay(self, stream_name: str, provider_pubkey: str,
                     relay_url: str):
        """Switch a subscription to a different relay."""
        conn = self._get_conn()
        conn.execute("""
            UPDATE subscriptions SET relay_url = ?, stale_since = NULL
            WHERE stream_name = ? AND provider_pubkey = ? AND active = 1
        """, (relay_url, stream_name, provider_pubkey))
        conn.commit()

    def should_recheck_stale(self, stale_since: int,
                             interval: int = 86400) -> bool:
        """Check if enough time has passed to recheck a stale stream."""
        if stale_since is None:
            return True
        return (int(time.time()) - stale_since) >= interval

    # ── Observations ───────────────────────────────────────────────

    def save_observation(self, stream_name: str, provider_pubkey: str,
                         value: str = None, event_id: str = None):
        """Record a received observation."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO observations
                (stream_name, provider_pubkey, received_at, value, event_id)
            VALUES (?, ?, ?, ?, ?)
        """, (stream_name, provider_pubkey, int(time.time()), value, event_id))
        conn.commit()

    def last_observation_time(self, stream_name: str,
                              provider_pubkey: str) -> int | None:
        """Get the timestamp of the last received observation for a stream."""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT received_at FROM observations
            WHERE stream_name = ? AND provider_pubkey = ?
            ORDER BY received_at DESC LIMIT 1
        """, (stream_name, provider_pubkey)).fetchone()
        return row['received_at'] if row else None

    def is_locally_stale(self, stream_name: str, provider_pubkey: str,
                         cadence_seconds: int,
                         multiplier: float = 1.5) -> bool:
        """Check if a subscribed stream is stale based on local observations.

        Compares last received observation time against the stream's cadence.
        Returns True if we haven't received an observation within
        cadence * multiplier seconds, or if we've never received one.
        """
        last = self.last_observation_time(stream_name, provider_pubkey)
        if last is None:
            return True  # never received — stale
        elapsed = int(time.time()) - last
        if cadence_seconds is None or cadence_seconds <= 0:
            return elapsed > 86400  # irregular: stale after 24h
        return elapsed > (cadence_seconds * multiplier)
