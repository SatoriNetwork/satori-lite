"""
SQLite database manager for engine data storage.
"""
import sqlite3
import os
import threading
from typing import Optional
import pandas as pd
from satorilib.logging import INFO, setup, debug, info, warning, error

setup(level=INFO)

SQLITE_BUSY_TIMEOUT_MS = 30000


class EngineSqliteDatabase:
    """SQLite database for storing stream and prediction data in the engine."""

    def __init__(self, data_dir: str = '/Satori/Engine/db', dbname: str = 'engine.db'):
        self.conn = None
        self._lock = threading.RLock()
        self.data_dir = data_dir
        self.dbname = os.path.join(data_dir, dbname)
        self.createConnection()

    def createConnection(self):
        """Creates or reopens a SQLite database connection with specific pragmas."""
        with self._lock:
            try:
                if self.conn:
                    self.conn.close()
                debug(f"Engine DB: Connecting to database at: {self.dbname}")
                os.makedirs(os.path.dirname(self.dbname), exist_ok=True)
                self.conn = sqlite3.connect(
                    self.dbname,
                    timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
                    check_same_thread=False)
                self.conn.execute(f'PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS};')
                self.conn.execute('PRAGMA foreign_keys = ON;')
                self.conn.execute('PRAGMA journal_mode = WAL;')
                self.conn.execute('PRAGMA synchronous = NORMAL;')
                self.conn.execute('PRAGMA wal_autocheckpoint = 100;')
                # Ensure metadata table exists even on a brand new engine.db.
                self._createStreamsTableLocked()
                info(f"Engine DB: Connected to {self.dbname}", color='green')
            except Exception as e:
                error(f"Engine DB: Connection error: {e}")

    def disconnect(self):
        """Closes the current database connection if one exists."""
        with self._lock:
            if self.conn:
                self.conn.close()
                self.conn = None

    def createTable(self, table_uuid: str):
        """Create table with schema: ts, value, hash, provider. Table name is the streamUUID."""
        with self._lock:
            try:
                cur = self.conn.cursor()
                cur.execute(f'''
                    CREATE TABLE IF NOT EXISTS "{table_uuid}" (
                        ts TIMESTAMP PRIMARY KEY NOT NULL,
                        value NUMERIC(20, 10) NOT NULL,
                        hash TEXT NOT NULL,
                        provider TEXT NOT NULL
                    )
                ''')
                self.conn.commit()
                debug(f"Engine DB: Created/verified table {table_uuid}")
            except Exception as e:
                error(f"Engine DB: Table creation error for {table_uuid}: {e}")

    def _tableExistsLocked(self, table_uuid: str) -> bool:
        """Check if a table exists. Caller must hold self._lock."""
        try:
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name=?
                """, (table_uuid,))
            return cur.fetchone() is not None
        except Exception as e:
            error(f"Engine DB: Error checking table existence: {e}")
            return False

    def migrateTimestampFormat(self, table_uuid: str) -> int:
        """
        Migrate datetime string timestamps to Unix timestamps.
        Handles backward compatibility for existing user data.

        Returns: Number of rows migrated
        """
        with self._lock:
            try:
                if not self._tableExistsLocked(table_uuid):
                    return 0

                cur = self.conn.cursor()
                cur.execute(f'SELECT ts, value, hash, provider FROM "{table_uuid}"')
                rows = cur.fetchall()

                migrated = 0
                for ts_val, value, hash_val, provider in rows:
                    if isinstance(ts_val, str) and ('-' in ts_val or ':' in ts_val):
                        try:
                            if '.' in ts_val:
                                dt = pd.to_datetime(ts_val, format='%Y-%m-%d %H:%M:%S.%f')
                            else:
                                dt = pd.to_datetime(ts_val, format='%Y-%m-%d %H:%M:%S')

                            unix_ts = dt.timestamp()

                            cur.execute(f'DELETE FROM "{table_uuid}" WHERE ts = ?', (ts_val,))
                            cur.execute(
                                f'INSERT INTO "{table_uuid}" (ts, value, hash, provider) VALUES (?, ?, ?, ?)',
                                (unix_ts, value, hash_val, provider))

                            migrated += 1
                        except Exception as e:
                            warning(f"Engine DB: Could not migrate timestamp {ts_val}: {e}")

                if migrated > 0:
                    self.conn.commit()
                    info(f"Engine DB: Migrated {migrated} datetime strings to Unix timestamps in {table_uuid}", color='green')

                return migrated
            except Exception as e:
                error(f"Engine DB: Error during timestamp migration for {table_uuid}: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return 0

    def getTableData(self, table_uuid: str) -> pd.DataFrame:
        """Get all data from a table as DataFrame."""
        with self._lock:
            try:
                if not self._tableExistsLocked(table_uuid):
                    return pd.DataFrame(columns=['ts', 'value', 'hash', 'provider'])

                # Auto-migrate datetime strings to Unix timestamps on read.
                # Lock is reentrant (RLock), safe to call.
                self.migrateTimestampFormat(table_uuid)

                df = pd.read_sql_query(
                    f"""
                    SELECT ts, value, hash, provider
                    FROM "{table_uuid}"
                    ORDER BY ts
                    """,
                    self.conn,
                    index_col='ts')
                return df
            except Exception as e:
                error(f"Engine DB: Error reading table {table_uuid}: {e}")
                return pd.DataFrame(columns=['ts', 'value', 'hash', 'provider'])

    def insertRow(self, table_uuid: str, ts: str, value: float, hash_val: str, provider: str) -> bool:
        """Insert a single row into the table."""
        with self._lock:
            try:
                if not self._tableExistsLocked(table_uuid):
                    self.createTable(table_uuid)

                cur = self.conn.cursor()
                cur.execute(
                    f'SELECT 1 FROM "{table_uuid}" WHERE ts = ?', (ts,))
                if cur.fetchone():
                    debug(f"Engine DB: Row with ts={ts} already exists in {table_uuid}")
                    return False

                cur.execute(
                    f'''INSERT INTO "{table_uuid}" (ts, value, hash, provider)
                    VALUES (?, ?, ?, ?)''',
                    (ts, float(value), str(hash_val), str(provider)))
                self.conn.commit()
                debug(f"Engine DB: Inserted row into {table_uuid}")
                return True
            except Exception as e:
                error(f"Engine DB: Insert error for {table_uuid}: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return False

    def listTables(self) -> list:
        """List all tables in the database."""
        with self._lock:
            try:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
                return [row[0] for row in cur.fetchall()]
            except Exception as e:
                error(f"Engine DB: Error listing tables: {e}")
                return []

    # ==================== Streams Metadata Methods ====================

    def _createStreamsTableLocked(self):
        """Create the streams metadata table if it doesn't exist. Caller must hold self._lock."""
        try:
            cur = self.conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS streams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_stream_id INTEGER,
                    uuid TEXT UNIQUE NOT NULL,
                    name TEXT,
                    author TEXT,
                    secondary TEXT,
                    target TEXT,
                    meta TEXT,
                    description TEXT,
                    last_synced TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_streams_uuid ON streams(uuid)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_streams_name ON streams(name)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_streams_server_id ON streams(server_stream_id)')
            self.conn.commit()
            debug("Engine DB: Created/verified streams metadata table")
        except Exception as e:
            error(f"Engine DB: Error creating streams table: {e}")

    def upsertStream(
        self,
        uuid: str,
        server_stream_id: Optional[int] = None,
        name: Optional[str] = None,
        author: Optional[str] = None,
        secondary: Optional[str] = None,
        target: Optional[str] = None,
        meta: Optional[str] = None,
        description: Optional[str] = None
    ) -> bool:
        """
        Insert or update stream metadata.

        Args:
            uuid: Stream UUID (required, unique)
            server_stream_id: Stream ID from central server
            name: Human-readable stream name
            author: Wallet pubkey of peer allowed to publish on this stream
            secondary: Secondary identifier
            target: Target field for StreamId compatibility
            meta: Metadata field
            description: Stream description

        Returns:
            True if successful
        """
        with self._lock:
            try:
                from datetime import datetime
                last_synced = datetime.utcnow().isoformat()

                cur = self.conn.cursor()
                cur.execute('''
                    INSERT OR REPLACE INTO streams
                    (uuid, server_stream_id, name, author, secondary, target, meta, description, last_synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (uuid, server_stream_id, name, author, secondary, target, meta, description, last_synced))
                self.conn.commit()
                debug(f"Engine DB: Upserted stream metadata for {uuid} (name: {name}, author: {author})")
                return True
            except Exception as e:
                error(f"Engine DB: Error upserting stream {uuid}: {e}")
                return False

    def getAllStreams(self) -> list:
        """
        Get all streams metadata.

        Returns:
            List of dicts with stream info
        """
        with self._lock:
            try:
                cur = self.conn.cursor()
                cur.execute(
                    '''SELECT id, server_stream_id, uuid, name, author, secondary, target, meta, description, last_synced, created_at
                    FROM streams ORDER BY created_at DESC''')
                rows = cur.fetchall()
                return [{
                    'id': row[0],
                    'server_stream_id': row[1],
                    'uuid': row[2],
                    'name': row[3],
                    'author': row[4],
                    'secondary': row[5],
                    'target': row[6],
                    'meta': row[7],
                    'description': row[8],
                    'last_synced': row[9],
                    'created_at': row[10]
                } for row in rows]
            except Exception as e:
                error(f"Engine DB: Error getting all streams: {e}")
                return []
