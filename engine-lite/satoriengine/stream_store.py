"""Port-ready per-stream observation store for the central prediction path.

A single SQLite table holds every stream's history. Timestamps are stored as a
float unix epoch — the same form Central sends — so there is no string
round-trip and no re-parsing. The epoch -> datetime64 conversion happens
exactly once, in ``history()``, when the engine frame is built.

This is the efficient replacement for the table-per-stream / epoch-string
design in ``storage/sqlite_manager.py``. It is not yet wired into the neuron;
the engine testing ground proves it first.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

# The engine / adapters consume frames keyed on these columns.
ENGINE_COLUMNS = ['date_time', 'value', 'id']


class StreamStore:
    """SQLite-backed accumulator of per-stream observation history."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
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
        return self._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE stream_uuid = ?",
            (stream_uuid,)).fetchone()[0]

    def stream_uuids(self) -> list[str]:
        """All stream UUIDs that have stored data."""
        rows = self._conn.execute(
            "SELECT DISTINCT stream_uuid FROM observations").fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._conn.close()
