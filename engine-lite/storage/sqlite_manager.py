"""
SQLite database manager for engine data storage.
Follows patterns from lib-lite/satorilib/sqlite/sqlite_manager.py
"""
import sqlite3
import os
import hashlib
from typing import Union, Optional
import pandas as pd
from satorilib.logging import INFO, setup, debug, info, warning, error

setup(level=INFO)


class EngineSqliteDatabase:
    """SQLite database for storing stream and prediction data in the engine."""

    def __init__(self, data_dir: str = '/Satori/Engine/db', dbname: str = 'engine.db'):
        self.conn = None
        self.cursor = None
        self.data_dir = data_dir
        self.dbname = os.path.join(data_dir, dbname)
        self.createConnection()

    def createConnection(self):
        """Creates or reopens a SQLite database connection with specific pragmas."""
        try:
            if self.conn:
                self.conn.close()
            debug(f"Engine DB: Connecting to database at: {self.dbname}")
            os.makedirs(os.path.dirname(self.dbname), exist_ok=True)
            self.conn = sqlite3.connect(self.dbname, check_same_thread=False)
            self.cursor = self.conn.cursor()
            self.cursor.execute('PRAGMA foreign_keys = ON;')
            self.cursor.execute('PRAGMA journal_mode = WAL;')
            self.cursor.execute('PRAGMA wal_autocheckpoint = 100;')
            info(f"Engine DB: Connected to {self.dbname}", color='green')
        except Exception as e:
            error(f"Engine DB: Connection error: {e}")

    def disconnect(self):
        """Closes the current database connection if one exists."""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()

    def createTable(self, table_uuid: str):
        """Create table with schema: ts, value, hash, provider. Table name is the streamUUID."""
        try:
            self.cursor.execute(f'''
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

    def tableExists(self, table_uuid: str) -> bool:
        """Check if a table exists in the database."""
        try:
            self.cursor.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name=?
                """, (table_uuid,))
            return self.cursor.fetchone() is not None
        except Exception as e:
            error(f"Engine DB: Error checking table existence: {e}")
            return False

    def getTableData(self, table_uuid: str) -> pd.DataFrame:
        """Get all data from a table as DataFrame."""
        try:
            if not self.tableExists(table_uuid):
                return pd.DataFrame(columns=['ts', 'value', 'hash', 'provider'])

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
        try:
            if not self.tableExists(table_uuid):
                self.createTable(table_uuid)

            # Check if row already exists
            self.cursor.execute(
                f'SELECT 1 FROM "{table_uuid}" WHERE ts = ?', (ts,))
            if self.cursor.fetchone():
                debug(f"Engine DB: Row with ts={ts} already exists in {table_uuid}")
                return False

            self.cursor.execute(
                f'''INSERT INTO "{table_uuid}" (ts, value, hash, provider)
                VALUES (?, ?, ?, ?)''',
                (ts, float(value), str(hash_val), str(provider)))
            self.conn.commit()
            debug(f"Engine DB: Inserted row into {table_uuid}")
            return True
        except Exception as e:
            error(f"Engine DB: Insert error for {table_uuid}: {e}")
            self.conn.rollback()
            return False

    def insertDataframe(self, table_uuid: str, df: pd.DataFrame, provider: str = 'central') -> int:
        """
        Insert DataFrame into table. Returns number of rows inserted.
        DataFrame should have 'value' column and optionally 'hash'.
        Index should be timestamp.
        """
        try:
            if not self.tableExists(table_uuid):
                self.createTable(table_uuid)

            if df.empty:
                return 0

            # Ensure we have required columns
            if 'value' not in df.columns:
                error("Engine DB: DataFrame must have 'value' column")
                return 0

            inserted = 0
            for idx, row in df.iterrows():
                ts = str(idx)
                value = float(row['value'])

                # Use hash from df if available, otherwise generate
                if 'hash' in df.columns:
                    hash_val = str(row['hash'])
                else:
                    # Generate hash: blake2s(previous_hash + ts + value)
                    last_hash = self.getLastHash(table_uuid) or ''
                    hash_val = self.hashIt(last_hash + ts + str(value))

                prov = str(row['provider']) if 'provider' in df.columns else provider

                # Check for duplicates
                self.cursor.execute(
                    f'SELECT 1 FROM "{table_uuid}" WHERE ts = ?', (ts,))
                if not self.cursor.fetchone():
                    self.cursor.execute(
                        f'''INSERT INTO "{table_uuid}" (ts, value, hash, provider)
                        VALUES (?, ?, ?, ?)''',
                        (ts, value, hash_val, prov))
                    inserted += 1

            self.conn.commit()
            if inserted > 0:
                info(f"Engine DB: Inserted {inserted} rows into {table_uuid}")
            return inserted
        except Exception as e:
            error(f"Engine DB: DataFrame insert error for {table_uuid}: {e}")
            self.conn.rollback()
            return 0

    def getLastHash(self, table_uuid: str) -> Optional[str]:
        """Get the last hash from a table."""
        try:
            if not self.tableExists(table_uuid):
                return None
            self.cursor.execute(
                f'''SELECT hash FROM "{table_uuid}"
                ORDER BY ts DESC
                LIMIT 1''')
            result = self.cursor.fetchone()
            return result[0] if result else None
        except Exception as e:
            error(f"Engine DB: Error getting last hash from {table_uuid}: {e}")
            return None

    def getRowCount(self, table_uuid: str) -> int:
        """Get number of rows in a table."""
        try:
            if not self.tableExists(table_uuid):
                return 0
            self.cursor.execute(f'SELECT COUNT(*) FROM "{table_uuid}"')
            result = self.cursor.fetchone()
            return result[0] if result else 0
        except Exception as e:
            error(f"Engine DB: Error getting row count for {table_uuid}: {e}")
            return 0

    def getLatestTimestamp(self, table_uuid: str) -> Optional[str]:
        """Get the latest timestamp from a table."""
        try:
            if not self.tableExists(table_uuid):
                return None
            self.cursor.execute(
                f'''SELECT ts FROM "{table_uuid}"
                ORDER BY ts DESC
                LIMIT 1''')
            result = self.cursor.fetchone()
            return result[0] if result else None
        except Exception as e:
            error(f"Engine DB: Error getting latest timestamp from {table_uuid}: {e}")
            return None

    def deleteTable(self, table_uuid: str):
        """Delete a table from the database."""
        try:
            self.cursor.execute(f'DROP TABLE IF EXISTS "{table_uuid}"')
            self.conn.commit()
            info(f"Engine DB: Deleted table {table_uuid}")
        except Exception as e:
            error(f"Engine DB: Error deleting table {table_uuid}: {e}")

    def listTables(self) -> list:
        """List all tables in the database."""
        try:
            self.cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
            return [row[0] for row in self.cursor.fetchall()]
        except Exception as e:
            error(f"Engine DB: Error listing tables: {e}")
            return []

    @staticmethod
    def hashIt(string: str) -> str:
        """Generate blake2s hash for data integrity."""
        return hashlib.blake2s(
            string.encode(),
            digest_size=8).hexdigest()
