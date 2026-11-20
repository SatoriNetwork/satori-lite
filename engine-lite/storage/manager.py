"""
High-level storage manager for engine.
Provides simple API for storing/retrieving stream and prediction data.
"""
import os
import pandas as pd
from typing import Optional
from .sqlite_manager import EngineSqliteDatabase
from satoriengine.stream_store import StreamStore
from satorilib.logging import INFO, setup

setup(level=INFO)


class EngineStorageManager:
    """
    High-level storage manager for engine data.
    Handles both subscription stream data and prediction data.
    Table names are streamUUIDs.
    """

    _instance = None

    @classmethod
    def getInstance(cls, data_dir: str = '/Satori/Engine/db', dbname: str = 'engine.db'):
        """Get singleton instance of storage manager."""
        if cls._instance is None:
            cls._instance = cls(data_dir, dbname)
        return cls._instance

    def __init__(self, data_dir: str = '/Satori/Engine/db', dbname: str = 'engine.db'):
        self.db = EngineSqliteDatabase(data_dir, dbname)
        self.stream_store = StreamStore(os.path.join(data_dir, dbname))

    # ==================== Stream Data Methods ====================

    def storeStreamData(self, streamUuid: str, df: pd.DataFrame, provider: str = 'central') -> int:
        """
        Store subscription stream data via StreamStore.

        Args:
            streamUuid: The stream UUID
            df: DataFrame with columns [epoch, value, id]

        Returns:
            Number of rows inserted
        """
        return self.stream_store.append(streamUuid, df)

    def getStreamData(self, streamUuid: str) -> pd.DataFrame:
        """A stream's observations from the StreamStore, oldest first: column
        value, index ts (epoch seconds), as the web routes chart them."""
        return self.stream_store.observations(streamUuid).set_index('epoch').rename_axis('ts')[['value']]

    def getStreamDataForEngine(self, streamUuid: str) -> pd.DataFrame:
        """
        Get stream data formatted for engine consumption.
        Returns DataFrame with columns: date_time (datetime64 UTC), value, id.
        Epoch -> datetime64 conversion happens once here via StreamStore.history().
        """
        return self.stream_store.history(streamUuid)

    def getStreamRowCount(self, streamUuid: str) -> int:
        """Get number of rows for a stream."""
        return self.stream_store.row_count(streamUuid)

    def getLatestStreamTimestamp(self, streamUuid: str) -> Optional[float]:
        """The newest observation's epoch seconds, None if it has none."""
        return self.stream_store.latest_epoch(streamUuid)

    # ==================== Prediction Data Methods ====================

    def storePrediction(
        self,
        predictionStreamUuid: str,
        timestamp: str,
        value: float,
        hash_val: str,
        provider: str = 'engine'
    ) -> bool:
        """
        Store a prediction.

        Args:
            predictionStreamUuid: The prediction stream UUID (table name)
            timestamp: Prediction timestamp
            value: Predicted value
            hash_val: Hash for data integrity
            provider: Usually 'engine'

        Returns:
            True if inserted successfully
        """
        return self.db.insertRow(predictionStreamUuid, timestamp, value, hash_val, provider)

    def getPredictions(self, predictionStreamUuid: str) -> pd.DataFrame:
        """Get all predictions for a prediction stream."""
        return self.db.getTableData(predictionStreamUuid)

    # ==================== Utility Methods ====================

    def listAllStreams(self) -> list:
        """The per-stream tables: each prediction stream's, and the observations
        from before the StreamStore (whose tables are not streams)."""
        return [t for t in self.db.listTables()
                if t not in ('streams', 'sqlite_sequence', 'observations', '_satori_migrations')]

    def close(self):
        """Close database connection."""
        self.db.disconnect()
