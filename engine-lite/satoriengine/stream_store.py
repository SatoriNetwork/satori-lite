"""Port-ready per-stream observation store for the central prediction path.

A single SQLite table holds every stream's history. Timestamps are stored as a
float unix epoch — the same form Central sends — so there is no string
round-trip and no re-parsing. The epoch -> datetime64 conversion happens
exactly once, in ``history()``, when the engine frame is built.

This is the efficient replacement for the table-per-stream / epoch-string
design in ``storage/sqlite_manager.py``. Wired into the neuron via
``storage/manager.py``; auto-migrates legacy tables on first startup.
"""

from __future__ import annotations

import sqlite3
import threading

import pandas as pd

# The engine / adapters consume frames keyed on these columns.
ENGINE_COLUMNS = ['date_time', 'value', 'id']

_EPOCH_MIN = 946684800   # year 2000
_EPOCH_MAX = 4102444800  # year 2100


def _parse_ts(raw) -> float | None:
    """Parse a legacy ts value to a float epoch. Returns None if unparseable."""
    try:
        epoch = float(raw)
        if _EPOCH_MIN < epoch < _EPOCH_MAX:
            return epoch
    except (TypeError, ValueError):
        pass
    try:
        return pd.Timestamp(raw).timestamp()
    except Exception:
        return None


class StreamStore:
    """SQLite-backed accumulator of per-stream observation history."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                stream_uuid TEXT NOT NULL,
                epoch       REAL NOT NULL,
                value       REAL NOT NULL,
                id          TEXT,
                PRIMARY KEY (stream_uuid, epoch)
            )
        """)
        self._conn.commit()
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        """One-time migration from the old per-stream-table schema.

        Runs only once — completion is recorded in a _satori_migrations
        sentinel table so subsequent startups skip immediately.
        INSERT OR IGNORE deduplication means a restart mid-migration is safe.
        Legacy tables are left in place so a Docker image rollback still works.
        """
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS _satori_migrations "
                "(name TEXT PRIMARY KEY)")
            already_done = self._conn.execute(
                "SELECT 1 FROM _satori_migrations WHERE name='legacy_tables'"
            ).fetchone()
            if already_done:
                return

            all_tables = [
                row[0] for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' "
                    "AND name NOT IN ('observations', '_satori_migrations')"
                ).fetchall()
            ]

            # Identify observation tables only — skip prediction tables
            # (provider='engine') and non-stream tables (no ts/value/hash cols).
            obs_tables = []
            pred_uuids_to_clean = []
            for table in all_tables:
                try:
                    cols = {r[1] for r in self._conn.execute(
                        f'PRAGMA table_info("{table}")')}
                    if not {'ts', 'value', 'hash', 'provider'}.issubset(cols):
                        continue
                    providers = {r[0] for r in self._conn.execute(
                        f'SELECT DISTINCT provider FROM "{table}"')}
                    if 'engine' in providers:
                        pred_uuids_to_clean.append(table)
                    else:
                        obs_tables.append(table)
                except Exception:
                    continue

            # Remove any prediction rows that a previous migration run may have
            # incorrectly inserted into the observations table.
            for uuid in pred_uuids_to_clean:
                self._conn.execute(
                    "DELETE FROM observations WHERE stream_uuid = ?", (uuid,))

            if not obs_tables:
                self._conn.execute(
                    "INSERT OR IGNORE INTO _satori_migrations (name) "
                    "VALUES ('legacy_tables')")
                self._conn.commit()
                return

            total_migrated = 0
            for table in obs_tables:
                try:
                    rows = self._conn.execute(
                        f'SELECT ts, value, hash FROM "{table}"'
                    ).fetchall()
                except Exception:
                    continue

                before = self._conn.total_changes
                for ts_raw, value, hash_val in rows:
                    epoch = _parse_ts(ts_raw)
                    if epoch is None:
                        continue
                    try:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO observations "
                            "(stream_uuid, epoch, value, id) VALUES (?, ?, ?, ?)",
                            (table, epoch, float(value),
                             str(hash_val) if hash_val else None))
                    except Exception:
                        continue
                total_migrated += self._conn.total_changes - before

            self._conn.execute(
                "INSERT OR IGNORE INTO _satori_migrations (name) "
                "VALUES ('legacy_tables')")
            self._conn.commit()
            if total_migrated:
                print(f'StreamStore: migrated {total_migrated} rows '
                      f'from {len(obs_tables)} legacy stream table(s)')

    def append(self, stream_uuid: str, df: pd.DataFrame) -> int:
        """Append a storage-shaped frame (columns: epoch, value, id) for one
        stream. Duplicate ``(stream_uuid, epoch)`` rows are ignored — the
        composite primary key handles dedup, no hashing needed. Returns the
        number of rows actually inserted.
        """
        if df.empty:
            return 0
        rows = [
            (stream_uuid, float(r.epoch), float(r.value),
             None if r.id is None else str(r.id))
            for r in df.itertuples(index=False)
        ]
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO observations "
                "(stream_uuid, epoch, value, id) VALUES (?, ?, ?, ?)",
                rows)
            self._conn.commit()
            return self._conn.total_changes - before

    def history(self, stream_uuid: str) -> pd.DataFrame:
        """Return a stream's full history as the engine-ready frame —
        columns ``[date_time, value, id]``, sorted oldest-first.

        This is the single epoch -> datetime64 conversion point.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT epoch, value, id FROM observations "
                "WHERE stream_uuid = ? ORDER BY epoch",
                (stream_uuid,)).fetchall()
        if not rows:
            return pd.DataFrame(columns=ENGINE_COLUMNS)
        df = pd.DataFrame(rows, columns=['epoch', 'value', 'id'])
        df['date_time'] = pd.to_datetime(df['epoch'], unit='s', utc=True)
        return df[ENGINE_COLUMNS]

    def row_count(self, stream_uuid: str) -> int:
        """Number of stored observations for a stream."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM observations WHERE stream_uuid = ?",
                (stream_uuid,)).fetchone()[0]

    def stream_uuids(self) -> list[str]:
        """All stream UUIDs that have stored data."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT stream_uuid FROM observations").fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
