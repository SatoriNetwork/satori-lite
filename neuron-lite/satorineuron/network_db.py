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
                seq_num INTEGER,
                observed_at INTEGER,
                received_at INTEGER NOT NULL,
                value TEXT,
                event_id TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_obs_stream
            ON observations(stream_name, provider_pubkey, received_at DESC)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS relays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                relay_url TEXT NOT NULL UNIQUE,
                first_seen INTEGER NOT NULL,
                last_active INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name TEXT NOT NULL UNIQUE,
                source_stream_name TEXT,
                source_provider_pubkey TEXT,
                name TEXT,
                description TEXT,
                cadence_seconds INTEGER,
                price_per_obs INTEGER NOT NULL DEFAULT 0,
                encrypted INTEGER NOT NULL DEFAULT 0,
                tags TEXT,
                active INTEGER DEFAULT 1,
                created_at INTEGER NOT NULL,
                last_published_at INTEGER,
                last_seq_num INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name TEXT NOT NULL,
                provider_pubkey TEXT NOT NULL,
                observation_seq INTEGER,
                value TEXT NOT NULL,
                observed_at INTEGER,
                created_at INTEGER NOT NULL,
                published INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pred_stream
            ON predictions(stream_name, provider_pubkey, created_at DESC)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS data_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name TEXT NOT NULL UNIQUE,
                name TEXT,
                description TEXT,
                url TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT 'GET',
                headers TEXT,
                cadence_seconds INTEGER NOT NULL,
                parser_type TEXT NOT NULL DEFAULT 'json_path',
                parser_config TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
        """)
        # Migration: add stale_since if missing (existing DBs)
        try:
            conn.execute("SELECT stale_since FROM subscriptions LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN stale_since INTEGER")
        # Migration: add last_seq_num if missing (existing DBs)
        try:
            conn.execute("SELECT last_seq_num FROM publications LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE publications "
                "ADD COLUMN last_seq_num INTEGER NOT NULL DEFAULT 0")
        # Migration: add price_per_obs, encrypted to publications
        try:
            conn.execute("SELECT price_per_obs FROM publications LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE publications "
                "ADD COLUMN price_per_obs INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "ALTER TABLE publications "
                "ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0")
        # Migration: add source fields to publications
        try:
            conn.execute(
                "SELECT source_stream_name FROM publications LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE publications "
                "ADD COLUMN source_stream_name TEXT")
            conn.execute(
                "ALTER TABLE publications "
                "ADD COLUMN source_provider_pubkey TEXT")
        # Migration: add seq_num, observed_at to observations
        try:
            conn.execute("SELECT seq_num FROM observations LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE observations ADD COLUMN seq_num INTEGER")
            conn.execute(
                "ALTER TABLE observations ADD COLUMN observed_at INTEGER")
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
        self.upsert_relay(relay_url)
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

    def get_all(self) -> list[dict]:
        """Return all subscriptions including soft-deleted."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY active DESC, subscribed_at DESC"
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
        self.upsert_relay(relay_url)

    def should_recheck_stale(self, stale_since: int,
                             interval: int = 86400) -> bool:
        """Check if enough time has passed to recheck a stale stream."""
        if stale_since is None:
            return True
        return (int(time.time()) - stale_since) >= interval

    # ── Observations ───────────────────────────────────────────────

    def save_observation(self, stream_name: str, provider_pubkey: str,
                         value: str = None, event_id: str = None,
                         seq_num: int = None, observed_at: int = None):
        """Record a received observation. Skips if event_id already exists."""
        conn = self._get_conn()
        if event_id:
            existing = conn.execute(
                "SELECT 1 FROM observations WHERE event_id = ?",
                (event_id,)).fetchone()
            if existing:
                return
        conn.execute("""
            INSERT INTO observations
                (stream_name, provider_pubkey, seq_num, observed_at,
                 received_at, value, event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (stream_name, provider_pubkey, seq_num, observed_at,
              int(time.time()), value, event_id))
        conn.commit()

    def get_observations(self, stream_name: str, provider_pubkey: str,
                         limit: int = 50) -> list[dict]:
        """Return recent observations for a stream."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM observations
            WHERE stream_name = ? AND provider_pubkey = ?
            ORDER BY received_at DESC LIMIT ?
        """, (stream_name, provider_pubkey, limit)).fetchall()
        return [dict(r) for r in rows]

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
            return False  # no cadence = always considered live
        return elapsed > (cadence_seconds * multiplier)

    # ── Relays ────────────────────────────────────────────────────

    def upsert_relay(self, relay_url: str):
        """Record a relay, updating last_active if it already exists."""
        now = int(time.time())
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO relays (relay_url, first_seen, last_active)
            VALUES (?, ?, ?)
            ON CONFLICT(relay_url) DO UPDATE SET last_active = ?
        """, (relay_url, now, now, now))
        conn.commit()

    def get_relays(self) -> list[dict]:
        """Return all known relays."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM relays ORDER BY last_active DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_relay(self, relay_url: str):
        """Remove a relay from the known list."""
        conn = self._get_conn()
        conn.execute("DELETE FROM relays WHERE relay_url = ?", (relay_url,))
        conn.commit()

    # ── Publications ──────────────────────────────────────────────

    def add_publication(self, stream_name: str, name: str = '',
                        description: str = '',
                        cadence_seconds: int = None,
                        price_per_obs: int = 0,
                        encrypted: bool = False,
                        tags: list[str] = None,
                        source_stream_name: str = None,
                        source_provider_pubkey: str = None) -> int:
        """Register a stream we intend to publish. Returns row id."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO publications
                (stream_name, source_stream_name, source_provider_pubkey,
                 name, description, cadence_seconds, price_per_obs,
                 encrypted, tags, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(stream_name) DO UPDATE SET
                active = 1,
                source_stream_name = excluded.source_stream_name,
                source_provider_pubkey = excluded.source_provider_pubkey,
                name = excluded.name,
                description = excluded.description,
                cadence_seconds = excluded.cadence_seconds,
                price_per_obs = excluded.price_per_obs,
                encrypted = excluded.encrypted,
                tags = excluded.tags
        """, (
            stream_name, source_stream_name, source_provider_pubkey,
            name, description,
            cadence_seconds, price_per_obs,
            1 if encrypted else 0,
            ','.join(tags or []),
            int(time.time()),
        ))
        conn.commit()
        return conn.execute(
            "SELECT id FROM publications WHERE stream_name=?",
            (stream_name,)
        ).fetchone()[0]

    def remove_publication(self, stream_name: str):
        """Soft-delete a publication."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE publications SET active = 0 WHERE stream_name = ?",
            (stream_name,))
        conn.commit()

    def is_predicting(self, source_stream_name: str,
                      source_provider_pubkey: str) -> bool:
        """Check if we have an active prediction publication for a source stream."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT active FROM publications "
            "WHERE source_stream_name = ? AND source_provider_pubkey = ? "
            "AND active = 1",
            (source_stream_name, source_provider_pubkey)).fetchone()
        return row is not None

    def get_active_publications(self) -> list[dict]:
        """Return all active publications."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM publications WHERE active = 1 ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_publications(self) -> list[dict]:
        """Return all publications including soft-deleted."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM publications ORDER BY active DESC, created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_published(self, stream_name: str) -> int:
        """Bump seq_num and update last_published_at. Returns new seq_num."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE publications SET last_published_at = ?, "
            "last_seq_num = last_seq_num + 1 WHERE stream_name = ?",
            (int(time.time()), stream_name))
        conn.commit()
        row = conn.execute(
            "SELECT last_seq_num FROM publications WHERE stream_name = ?",
            (stream_name,)).fetchone()
        return row['last_seq_num'] if row else 0

    # ── Predictions ──────────────────────────────────────────────

    def save_prediction(self, stream_name: str, provider_pubkey: str,
                        value: str, observation_seq: int = None,
                        observed_at: int = None) -> int:
        """Save a prediction. Returns row id."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO predictions
                (stream_name, provider_pubkey, observation_seq,
                 value, observed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            stream_name, provider_pubkey, observation_seq,
            value, observed_at, int(time.time()),
        ))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_predictions(self, stream_name: str,
                        provider_pubkey: str = None,
                        limit: int = 100) -> list[dict]:
        """Return recent predictions for a stream."""
        conn = self._get_conn()
        if provider_pubkey:
            rows = conn.execute("""
                SELECT * FROM predictions
                WHERE stream_name = ? AND provider_pubkey = ?
                ORDER BY created_at DESC LIMIT ?
            """, (stream_name, provider_pubkey, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM predictions
                WHERE stream_name = ?
                ORDER BY created_at DESC LIMIT ?
            """, (stream_name, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_unpublished_predictions(self) -> list[dict]:
        """Return predictions not yet published."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM predictions
            WHERE published = 0
            ORDER BY created_at ASC
        """).fetchall()
        return [dict(r) for r in rows]

    def mark_prediction_published(self, prediction_id: int):
        """Mark a prediction as published."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE predictions SET published = 1 WHERE id = ?",
            (prediction_id,))
        conn.commit()

    # ── Data Sources ─────────────────────────────────────────────

    def add_data_source(self, stream_name: str, url: str = '',
                        cadence_seconds: int = 0, parser_type: str = '',
                        parser_config: str = '', name: str = '',
                        description: str = '', method: str = 'GET',
                        headers: str = None) -> int:
        """Register an external data source. Returns row id."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO data_sources
                (stream_name, name, description, url, method, headers,
                 cadence_seconds, parser_type, parser_config, active,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(stream_name) DO UPDATE SET
                active = 1,
                name = excluded.name,
                description = excluded.description,
                url = excluded.url,
                method = excluded.method,
                headers = excluded.headers,
                cadence_seconds = excluded.cadence_seconds,
                parser_type = excluded.parser_type,
                parser_config = excluded.parser_config
        """, (
            stream_name, name, description, url, method, headers,
            cadence_seconds, parser_type, parser_config,
            int(time.time()),
        ))
        conn.commit()
        return conn.execute(
            "SELECT id FROM data_sources WHERE stream_name=?",
            (stream_name,)).fetchone()[0]

    def remove_data_source(self, stream_name: str):
        """Soft-delete a data source."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE data_sources SET active = 0 WHERE stream_name = ?",
            (stream_name,))
        conn.commit()

    def get_active_data_sources(self) -> list[dict]:
        """Return all active data sources."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM data_sources WHERE active = 1 "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_data_sources(self) -> list[dict]:
        """Return all data sources including soft-deleted."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM data_sources "
            "ORDER BY active DESC, created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_data_source(self, stream_name: str) -> dict | None:
        """Return a single data source by stream_name."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM data_sources WHERE stream_name = ?",
            (stream_name,)).fetchone()
        return dict(row) if row else None
