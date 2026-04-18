"""Local SQLite storage for network datastream subscriptions."""

import os
import sqlite3
import threading
import time
from typing import Optional


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
                stream_name          TEXT NOT NULL,
                relay_url            TEXT NOT NULL,
                provider_pubkey      TEXT NOT NULL,
                provider_wallet_pubkey TEXT,
                name                 TEXT,
                description          TEXT,
                cadence_seconds      INTEGER,
                price_per_obs        INTEGER DEFAULT 0,
                encrypted            INTEGER DEFAULT 0,
                tags                 TEXT,
                active               INTEGER DEFAULT 1,
                subscribed_at        INTEGER NOT NULL,
                unsubscribed_at      INTEGER,
                stale_since          INTEGER,
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
                last_active INTEGER NOT NULL,
                user_added INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migration: add user_added column if missing (existing DBs)
        try:
            conn.execute(
                "ALTER TABLE relays ADD COLUMN user_added "
                "INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
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
        # Migration: add provider_wallet_pubkey if missing (existing DBs)
        try:
            conn.execute(
                "SELECT provider_wallet_pubkey FROM subscriptions LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE subscriptions "
                "ADD COLUMN provider_wallet_pubkey TEXT")
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                p2sh_address          TEXT PRIMARY KEY,
                sender_pubkey         TEXT NOT NULL,
                receiver_pubkey       TEXT NOT NULL,
                redeem_script         TEXT NOT NULL,
                funding_txid          TEXT NOT NULL,
                funding_vout          INTEGER NOT NULL,
                locked_sats           INTEGER NOT NULL,
                remainder_sats        INTEGER NOT NULL,
                blocks                INTEGER,
                minutes               REAL,
                is_sender             INTEGER NOT NULL,
                sender_nostr_pubkey   TEXT,
                receiver_nostr_pubkey TEXT,
                created_at            INTEGER NOT NULL,
                pending_commitment    TEXT
            )
        """)
        # Migration: add pending_commitment to existing DBs
        try:
            conn.execute("SELECT pending_commitment FROM channels LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN pending_commitment TEXT")
        # Migration: add sender_nostr_pubkey if missing (existing DBs)
        try:
            conn.execute("SELECT sender_nostr_pubkey FROM channels LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN sender_nostr_pubkey TEXT")
        # Migration: add receiver_nostr_pubkey if missing (existing DBs).
        # The sender needs this to tag commitment events with the receiver's
        # 32-byte Nostr pubkey (separate from the 33-byte EVR wallet pubkey
        # stored in receiver_pubkey).
        try:
            conn.execute("SELECT receiver_nostr_pubkey FROM channels LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN receiver_nostr_pubkey TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS competition_predictions (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name             TEXT NOT NULL,
                stream_provider_pubkey  TEXT NOT NULL,
                predictor_pubkey        TEXT NOT NULL,
                predictor_wallet_pubkey TEXT,
                host_pubkey             TEXT NOT NULL,
                seq_num                 INTEGER NOT NULL,
                predicted_value         TEXT NOT NULL,
                received_at             INTEGER NOT NULL,
                UNIQUE(stream_name, stream_provider_pubkey, predictor_pubkey, seq_num)
            )
        """)
        # Migration: add predictor_wallet_pubkey if missing (existing DBs)
        try:
            conn.execute(
                "SELECT predictor_wallet_pubkey FROM competition_predictions LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE competition_predictions "
                "ADD COLUMN predictor_wallet_pubkey TEXT")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_comp_pred_seq
            ON competition_predictions(stream_name, stream_provider_pubkey, seq_num)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS competitions (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name             TEXT NOT NULL,
                stream_provider_pubkey  TEXT NOT NULL,
                host_pubkey             TEXT NOT NULL,
                pay_per_obs_sats        INTEGER NOT NULL,
                paid_predictors         INTEGER NOT NULL,
                competing_predictors    INTEGER NOT NULL,
                scoring_metric          TEXT NOT NULL,
                scoring_params          TEXT NOT NULL DEFAULT '{}',
                horizon                 INTEGER NOT NULL DEFAULT 1,
                active                  INTEGER NOT NULL DEFAULT 1,
                timestamp               INTEGER NOT NULL,
                UNIQUE(stream_name, stream_provider_pubkey, host_pubkey)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS competition_payments (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name             TEXT NOT NULL,
                stream_provider_pubkey  TEXT NOT NULL,
                predictor_pubkey        TEXT NOT NULL,
                seq_num                 INTEGER NOT NULL,
                sats_paid               INTEGER NOT NULL,
                paid_at                 INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_comp_pay_stream
            ON competition_payments(stream_name, stream_provider_pubkey)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS joined_competitions (
                stream_name             TEXT NOT NULL,
                stream_provider_pubkey  TEXT NOT NULL,
                host_pubkey             TEXT NOT NULL,
                joined_at               INTEGER NOT NULL,
                PRIMARY KEY (stream_name, stream_provider_pubkey, host_pubkey)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_joined_comp_stream
            ON joined_competitions(stream_name, stream_provider_pubkey)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS access_requests (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name         TEXT NOT NULL,
                requester_pubkey    TEXT NOT NULL,
                message             TEXT DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'pending',
                requested_at        INTEGER NOT NULL,
                resolved_at         INTEGER,
                UNIQUE(stream_name, requester_pubkey)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_access_req_stream
            ON access_requests(stream_name, status)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS approved_subscribers (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name         TEXT NOT NULL,
                subscriber_pubkey   TEXT NOT NULL,
                approved_at         INTEGER NOT NULL,
                UNIQUE(stream_name, subscriber_pubkey)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_approved_sub_stream
            ON approved_subscribers(stream_name)
        """)
        # Migration: add approval_required to publications if missing
        try:
            conn.execute(
                "SELECT approval_required FROM publications LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE publications "
                "ADD COLUMN approval_required INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # ── Subscriptions ──────────────────────────────────────────────

    def subscribe(self, stream: dict, relay_url: str) -> int:
        """Subscribe to a stream. Returns row id."""
        conn = self._get_conn()
        tags = ','.join(stream.get('tags', []))
        wallet_pubkey = (stream.get('metadata') or {}).get('wallet_pubkey')
        conn.execute("""
            INSERT INTO subscriptions
                (stream_name, relay_url, provider_pubkey, provider_wallet_pubkey,
                 name, description, cadence_seconds, price_per_obs, encrypted,
                 tags, active, subscribed_at, stale_since)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL)
            ON CONFLICT(stream_name, provider_pubkey) DO UPDATE SET
                active = 1,
                relay_url = excluded.relay_url,
                provider_wallet_pubkey = COALESCE(excluded.provider_wallet_pubkey,
                                                   provider_wallet_pubkey),
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
            wallet_pubkey,
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
                         seq_num: int = None, observed_at: int = None) -> bool:
        """Record a received observation. Skips duplicates by event_id or seq_num.
        Returns True if a new row was inserted, False if skipped as duplicate."""
        conn = self._get_conn()
        if event_id:
            existing = conn.execute(
                "SELECT 1 FROM observations WHERE event_id = ?",
                (event_id,)).fetchone()
            if existing:
                return False
        if seq_num is not None:
            existing = conn.execute(
                "SELECT 1 FROM observations WHERE stream_name = ? AND provider_pubkey = ? AND seq_num = ?",
                (stream_name, provider_pubkey, seq_num)).fetchone()
            if existing:
                return False
        conn.execute("""
            INSERT INTO observations
                (stream_name, provider_pubkey, seq_num, observed_at,
                 received_at, value, event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (stream_name, provider_pubkey, seq_num, observed_at,
              int(time.time()), value, event_id))
        conn.commit()
        return True

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

    def get_observation_by_seq(self, stream_name: str, provider_pubkey: str,
                               seq_num: int) -> Optional[dict]:
        """Return a single observation row by (stream, provider, seq_num), or None."""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT * FROM observations
            WHERE stream_name = ? AND provider_pubkey = ? AND seq_num = ?
        """, (stream_name, provider_pubkey, seq_num)).fetchone()
        return dict(row) if row else None

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

    def upsert_relay(self, relay_url: str, user_added: bool = False):
        """Record a relay, updating last_active if it already exists."""
        now = int(time.time())
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO relays (relay_url, first_seen, last_active, user_added)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(relay_url) DO UPDATE SET
                last_active = ?,
                user_added = MAX(relays.user_added, ?)
        """, (relay_url, now, now, int(user_added), now, int(user_added)))
        conn.commit()

    def get_relays(self) -> list[dict]:
        """Return all known relays."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM relays ORDER BY last_active DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_user_added_relays(self) -> list[dict]:
        """Return relays manually added by the user."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM relays WHERE user_added = 1 "
            "ORDER BY last_active DESC"
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

    # ── Channels ──────────────────────────────────────────────────

    def save_channel(
        self,
        p2sh_address: str,
        sender_pubkey: str,
        receiver_pubkey: str,
        redeem_script: str,
        funding_txid: str,
        funding_vout: int,
        locked_sats: int,
        remainder_sats: int,
        is_sender: bool,
        blocks: int = None,
        minutes: float = None,
        sender_nostr_pubkey: str = None,
        receiver_nostr_pubkey: str = None,
        created_at: int = None,
    ) -> None:
        """Persist a payment channel. Upserts on p2sh_address.

        If created_at is provided, it is used as the CSV timer anchor. Callers
        on the receiver side should pass the sender's announcement timestamp so
        both sides agree on when the channel started — Nostr delivery delays
        would otherwise skew the receiver's clock ahead of the sender's.
        """
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO channels
                (p2sh_address, sender_pubkey, receiver_pubkey, redeem_script,
                 funding_txid, funding_vout, locked_sats, remainder_sats,
                 blocks, minutes, is_sender, sender_nostr_pubkey,
                 receiver_nostr_pubkey, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(p2sh_address) DO UPDATE SET
                redeem_script         = excluded.redeem_script,
                funding_txid          = excluded.funding_txid,
                funding_vout          = excluded.funding_vout,
                locked_sats           = excluded.locked_sats,
                remainder_sats        = excluded.remainder_sats,
                blocks                = excluded.blocks,
                minutes               = excluded.minutes,
                created_at            = excluded.created_at,
                sender_nostr_pubkey   = COALESCE(excluded.sender_nostr_pubkey,
                                                  sender_nostr_pubkey),
                receiver_nostr_pubkey = COALESCE(excluded.receiver_nostr_pubkey,
                                                  receiver_nostr_pubkey)
        """, (
            p2sh_address, sender_pubkey, receiver_pubkey, redeem_script,
            funding_txid, funding_vout, locked_sats, remainder_sats,
            blocks, minutes, 1 if is_sender else 0, sender_nostr_pubkey,
            receiver_nostr_pubkey,
            created_at if created_at is not None else int(time.time()),
        ))
        conn.commit()

    def store_pending_commitment(self, p2sh_address: str,
                                 commitment_json: str) -> None:
        """Store the latest received commitment for a channel (receiver side).

        Replaces any previously stored commitment — only the latest matters
        since it includes the cumulative remainder.
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE channels SET pending_commitment = ? WHERE p2sh_address = ?",
            (commitment_json, p2sh_address))
        conn.commit()

    def clear_pending_commitment(self, p2sh_address: str) -> None:
        """Clear the pending commitment after it has been claimed."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE channels SET pending_commitment = NULL WHERE p2sh_address = ?",
            (p2sh_address,))
        conn.commit()

    def get_channels_near_expiry(self, within_seconds: int = 86400) -> list[dict]:
        """Return receiver channels whose timeout expires within `within_seconds`.

        Used to auto-claim before the sender can reclaim the funds.
        Only returns channels that have a pending commitment to claim.
        Minutes-based timeout uses created_at + minutes*60 as the expiry epoch.
        Block-based timeout uses created_at + blocks*600 (approx 10 min/block).
        """
        now = int(time.time())
        cutoff = now + within_seconds
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM channels
            WHERE is_sender = 0
              AND pending_commitment IS NOT NULL
              AND (
                (minutes IS NOT NULL AND created_at + minutes * 60 <= ?)
                OR
                (blocks IS NOT NULL AND created_at + blocks * 600 <= ?)
              )
        """, (cutoff, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def get_channel(self, p2sh_address: str) -> dict | None:
        """Return a single channel by its P2SH address."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM channels WHERE p2sh_address = ?",
            (p2sh_address,)).fetchone()
        return dict(row) if row else None

    def get_channels_as_sender(self) -> list[dict]:
        """Return all channels where we are the sender (buyer)."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM channels WHERE is_sender = 1 ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_channels_as_receiver(self) -> list[dict]:
        """Return all channels where we are the receiver (seller)."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM channels WHERE is_sender = 0 ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_channel_remainder(self, p2sh_address: str,
                                 remainder_sats: int) -> None:
        """Update remaining balance after a commitment is claimed."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE channels SET remainder_sats = ? WHERE p2sh_address = ?",
            (remainder_sats, p2sh_address))
        conn.commit()

    def update_channel_funding(
        self,
        p2sh_address: str,
        funding_txid: str,
        funding_vout: int,
        locked_sats: int,
        created_at: int = None,
    ) -> None:
        """Update channel after a claim or refund creates a new P2SH UTXO.

        Resets locked_sats and remainder_sats to the new UTXO value so
        cumulative payment tracking restarts from zero. Also resets
        created_at since the CSV timer restarts with the new UTXO — pass
        the settlement/refund timestamp so sender and receiver agree.
        """
        conn = self._get_conn()
        ts = created_at if created_at is not None else int(time.time())
        conn.execute("""
            UPDATE channels SET
                funding_txid   = ?,
                funding_vout   = ?,
                locked_sats    = ?,
                remainder_sats = ?,
                created_at     = ?
            WHERE p2sh_address = ?
        """, (funding_txid, funding_vout, locked_sats, locked_sats,
              ts, p2sh_address))
        conn.commit()

    # ── Competitions ───────────────────────────────────────────────

    def add_competition(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        host_pubkey: str,
        pay_per_obs_sats: int,
        paid_predictors: int,
        competing_predictors: int,
        scoring_metric: str,
        scoring_params: str = '{}',
        horizon: int = 1,
        active: int = 1,
        timestamp: int = 0,
    ) -> None:
        """Insert or replace a competition announcement."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO competitions (
                stream_name, stream_provider_pubkey, host_pubkey,
                pay_per_obs_sats, paid_predictors, competing_predictors,
                scoring_metric, scoring_params, horizon, active, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stream_name, stream_provider_pubkey, host_pubkey)
            DO UPDATE SET
                pay_per_obs_sats     = excluded.pay_per_obs_sats,
                paid_predictors      = excluded.paid_predictors,
                competing_predictors = excluded.competing_predictors,
                scoring_metric       = excluded.scoring_metric,
                scoring_params       = excluded.scoring_params,
                horizon              = excluded.horizon,
                active               = excluded.active,
                timestamp            = excluded.timestamp
        """, (stream_name, stream_provider_pubkey, host_pubkey,
              pay_per_obs_sats, paid_predictors, competing_predictors,
              scoring_metric, scoring_params, horizon, active, timestamp))
        conn.commit()

    def get_competition(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        host_pubkey: str,
    ) -> dict | None:
        conn = self._get_conn()
        row = conn.execute("""
            SELECT * FROM competitions
            WHERE stream_name = ? AND stream_provider_pubkey = ? AND host_pubkey = ?
        """, (stream_name, stream_provider_pubkey, host_pubkey)).fetchone()
        return dict(row) if row else None

    def get_all_competitions(self, active_only: bool = False) -> list[dict]:
        conn = self._get_conn()
        if active_only:
            rows = conn.execute(
                "SELECT * FROM competitions WHERE active = 1").fetchall()
        else:
            rows = conn.execute("SELECT * FROM competitions").fetchall()
        return [dict(r) for r in rows]

    def get_competitions_hosted_by(self, host_pubkey: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM competitions WHERE host_pubkey = ?",
            (host_pubkey,)).fetchall()
        return [dict(r) for r in rows]

    def close_competition(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        host_pubkey: str,
    ) -> None:
        conn = self._get_conn()
        conn.execute("""
            UPDATE competitions SET active = 0
            WHERE stream_name = ? AND stream_provider_pubkey = ? AND host_pubkey = ?
        """, (stream_name, stream_provider_pubkey, host_pubkey))
        conn.commit()

    # ── Joined Competitions (predictor side) ───────────────────────

    def join_competition(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        host_pubkey: str,
    ) -> None:
        """Record that this neuron has joined a competition as a predictor.

        The (stream, host) pair is what the engine uses to know where to
        send its prediction DMs when it predicts for a given stream.
        Idempotent — re-joining the same competition does nothing.
        """
        conn = self._get_conn()
        conn.execute("""
            INSERT OR IGNORE INTO joined_competitions
                (stream_name, stream_provider_pubkey, host_pubkey, joined_at)
            VALUES (?, ?, ?, ?)
        """, (stream_name, stream_provider_pubkey, host_pubkey, int(time.time())))
        conn.commit()

    def leave_competition(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        host_pubkey: str,
    ) -> None:
        conn = self._get_conn()
        conn.execute("""
            DELETE FROM joined_competitions
            WHERE stream_name = ? AND stream_provider_pubkey = ? AND host_pubkey = ?
        """, (stream_name, stream_provider_pubkey, host_pubkey))
        conn.commit()

    def get_joined_competitions_for_stream(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
    ) -> list[dict]:
        """Return joined competitions for a stream — one row per host."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM joined_competitions
            WHERE stream_name = ? AND stream_provider_pubkey = ?
        """, (stream_name, stream_provider_pubkey)).fetchall()
        return [dict(r) for r in rows]

    def get_all_joined_competitions(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM joined_competitions ORDER BY joined_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Competition Predictions ────────────────────────────────────

    def save_competition_prediction(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        predictor_pubkey: str,
        host_pubkey: str,
        seq_num: int,
        predicted_value: str,
        received_at: int,
        predictor_wallet_pubkey: Optional[str] = None,
    ) -> None:
        """Save a received prediction, replacing any earlier one for this predictor+seq.

        predictor_wallet_pubkey is recorded so the host can open a payment
        channel to pay the predictor without needing a separate lookup.
        """
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO competition_predictions (
                stream_name, stream_provider_pubkey, predictor_pubkey,
                predictor_wallet_pubkey, host_pubkey, seq_num,
                predicted_value, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stream_name, stream_provider_pubkey, predictor_pubkey, seq_num)
            DO UPDATE SET
                predicted_value         = excluded.predicted_value,
                predictor_wallet_pubkey = COALESCE(
                    excluded.predictor_wallet_pubkey,
                    competition_predictions.predictor_wallet_pubkey),
                received_at             = excluded.received_at
            WHERE excluded.received_at > competition_predictions.received_at
        """, (stream_name, stream_provider_pubkey, predictor_pubkey,
              predictor_wallet_pubkey, host_pubkey, seq_num,
              predicted_value, received_at))
        conn.commit()

    def get_competition_predictions(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        seq_num: int,
    ) -> list[dict]:
        """Return all predictions received for a given stream+seq_num."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM competition_predictions
            WHERE stream_name = ? AND stream_provider_pubkey = ? AND seq_num = ?
        """, (stream_name, stream_provider_pubkey, seq_num)).fetchall()
        return [dict(r) for r in rows]

    # ── Competition Payments (accountability) ──────────────────────

    def record_competition_payment(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        predictor_pubkey: str,
        seq_num: int,
        sats_paid: int,
        paid_at: int,
    ) -> None:
        """Record a successful payment to a predictor after scoring."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO competition_payments
                (stream_name, stream_provider_pubkey, predictor_pubkey,
                 seq_num, sats_paid, paid_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (stream_name, stream_provider_pubkey, predictor_pubkey,
              seq_num, sats_paid, paid_at))
        conn.commit()

    def get_competition_payments(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
    ) -> list[dict]:
        """Return all payment records for a stream competition."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM competition_payments
            WHERE stream_name = ? AND stream_provider_pubkey = ?
            ORDER BY paid_at DESC
        """, (stream_name, stream_provider_pubkey)).fetchall()
        return [dict(r) for r in rows]

    def is_seq_already_scored(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        seq_num: int,
    ) -> bool:
        """Return True if any payment record exists for this seq_num."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM competition_payments "
            "WHERE stream_name = ? AND stream_provider_pubkey = ? "
            "AND seq_num = ? LIMIT 1",
            (stream_name, stream_provider_pubkey, seq_num)).fetchone()
        return row is not None

    def get_competition_leaderboard(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
    ) -> list[dict]:
        """Return per-predictor payment totals, sorted by total_sats descending."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT
                predictor_pubkey,
                SUM(sats_paid)  AS total_sats,
                COUNT(*)        AS prediction_count
            FROM competition_payments
            WHERE stream_name = ? AND stream_provider_pubkey = ?
            GROUP BY predictor_pubkey
            ORDER BY total_sats DESC
        """, (stream_name, stream_provider_pubkey)).fetchall()
        return [dict(r) for r in rows]

    def get_host_payment_stats(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        host_pubkey: str,
    ) -> dict | None:
        """Return payment consistency stats for a hosted competition.

        Returns None if no competition exists for this host.
        scored_observations counts distinct seq_nums that received payments.
        """
        conn = self._get_conn()
        comp = conn.execute("""
            SELECT pay_per_obs_sats FROM competitions
            WHERE stream_name = ? AND stream_provider_pubkey = ? AND host_pubkey = ?
        """, (stream_name, stream_provider_pubkey, host_pubkey)).fetchone()
        if not comp:
            return None
        row = conn.execute("""
            SELECT
                COALESCE(SUM(obs_total), 0)   AS total_paid_sats,
                COUNT(*)                       AS scored_observations,
                COALESCE(AVG(obs_total), 0.0)  AS avg_paid_per_obs
            FROM (
                SELECT seq_num, SUM(sats_paid) AS obs_total
                FROM competition_payments
                WHERE stream_name = ? AND stream_provider_pubkey = ?
                GROUP BY seq_num
            )
        """, (stream_name, stream_provider_pubkey)).fetchone()
        return {
            'total_paid_sats': row['total_paid_sats'],
            'scored_observations': row['scored_observations'],
            'avg_paid_per_obs': row['avg_paid_per_obs'],
            'announced_per_obs': comp['pay_per_obs_sats'],
        }

    # ── Access Requests (producer side) ───────────────────────────

    def add_access_request(
        self,
        stream_name: str,
        requester_pubkey: str,
        message: str = '',
        requested_at: int = 0,
    ) -> None:
        """Record an incoming access request. Re-requesting resets to pending."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO access_requests
                (stream_name, requester_pubkey, message, status, requested_at)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT(stream_name, requester_pubkey) DO UPDATE SET
                message      = excluded.message,
                status       = 'pending',
                requested_at = excluded.requested_at,
                resolved_at  = NULL
        """, (stream_name, requester_pubkey, message,
              requested_at or int(time.time())))
        conn.commit()

    def get_access_requests(
        self,
        stream_name: str,
        status: str | None = None,
    ) -> list[dict]:
        """Return access requests for a stream, optionally filtered by status."""
        conn = self._get_conn()
        if status:
            rows = conn.execute("""
                SELECT * FROM access_requests
                WHERE stream_name = ? AND status = ?
                ORDER BY requested_at DESC
            """, (stream_name, status)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM access_requests
                WHERE stream_name = ?
                ORDER BY requested_at DESC
            """, (stream_name,)).fetchall()
        return [dict(r) for r in rows]

    def get_all_pending_access_requests(self) -> list[dict]:
        """Return all pending access requests across all streams."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM access_requests
            WHERE status = 'pending'
            ORDER BY requested_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def approve_access_request(
        self,
        stream_name: str,
        requester_pubkey: str,
    ) -> None:
        """Approve a request and add to approved subscribers list."""
        conn = self._get_conn()
        now = int(time.time())
        conn.execute("""
            UPDATE access_requests SET status = 'approved', resolved_at = ?
            WHERE stream_name = ? AND requester_pubkey = ?
        """, (now, stream_name, requester_pubkey))
        conn.execute("""
            INSERT OR IGNORE INTO approved_subscribers
                (stream_name, subscriber_pubkey, approved_at)
            VALUES (?, ?, ?)
        """, (stream_name, requester_pubkey, now))
        conn.commit()

    def reject_access_request(
        self,
        stream_name: str,
        requester_pubkey: str,
    ) -> None:
        """Reject an access request."""
        conn = self._get_conn()
        conn.execute("""
            UPDATE access_requests SET status = 'rejected', resolved_at = ?
            WHERE stream_name = ? AND requester_pubkey = ?
        """, (int(time.time()), stream_name, requester_pubkey))
        conn.commit()

    def revoke_subscriber(
        self,
        stream_name: str,
        subscriber_pubkey: str,
    ) -> None:
        """Revoke a previously approved subscriber's access."""
        conn = self._get_conn()
        conn.execute("""
            DELETE FROM approved_subscribers
            WHERE stream_name = ? AND subscriber_pubkey = ?
        """, (stream_name, subscriber_pubkey))
        conn.execute("""
            UPDATE access_requests SET status = 'revoked', resolved_at = ?
            WHERE stream_name = ? AND requester_pubkey = ?
        """, (int(time.time()), stream_name, subscriber_pubkey))
        conn.commit()

    def is_subscriber_approved(
        self,
        stream_name: str,
        subscriber_pubkey: str,
    ) -> bool:
        """Check if a subscriber is on the approved list for a stream."""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT 1 FROM approved_subscribers
            WHERE stream_name = ? AND subscriber_pubkey = ?
        """, (stream_name, subscriber_pubkey)).fetchone()
        return row is not None

    def get_approved_subscribers(self, stream_name: str) -> list[dict]:
        """Return all approved subscribers for a stream."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM approved_subscribers
            WHERE stream_name = ?
            ORDER BY approved_at DESC
        """, (stream_name,)).fetchall()
        return [dict(r) for r in rows]

    def is_stream_approval_required(self, stream_name: str) -> bool:
        """Check if a published stream requires approval for access."""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT approval_required FROM publications
            WHERE stream_name = ? AND active = 1
        """, (stream_name,)).fetchone()
        return bool(row and row['approval_required'])
