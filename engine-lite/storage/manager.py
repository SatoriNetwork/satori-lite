"""
High-level storage manager for engine.
Provides simple API for storing/retrieving stream and prediction data.
"""
import pandas as pd
from typing import Optional, Union
from .sqlite_manager import EngineSqliteDatabase
from satorilib.logging import INFO, setup, debug, info, warning, error

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

    # ==================== Stream Data Methods ====================

    def storeStreamData(self, streamUuid: str, df: pd.DataFrame, provider: str = 'central') -> int:
        """
        Store subscription stream data.

        Args:
            streamUuid: The stream UUID (used as table name)
            df: DataFrame with 'value' column, index as timestamp
            provider: Data provider identifier

        Returns:
            Number of rows inserted
        """
        return self.db.insertDataframe(streamUuid, df, provider)

    def storeStreamObservation(
        self,
        streamUuid: str,
        timestamp: str,
        value: float,
        hash_val: str,
        provider: str = 'central'
    ) -> bool:
        """
        Store a single observation for a stream.

        Args:
            streamUuid: The stream UUID (table name)
            timestamp: Observation timestamp
            value: Observation value
            hash_val: Hash for data integrity
            provider: Data provider

        Returns:
            True if inserted successfully
        """
        return self.db.insertRow(streamUuid, timestamp, value, hash_val, provider)

    def getStreamData(self, streamUuid: str) -> pd.DataFrame:
        """
        Get all data for a stream.

        Args:
            streamUuid: The stream UUID

        Returns:
            DataFrame with columns: value, hash, provider (index: ts)
        """
        return self.db.getTableData(streamUuid)

    def getStreamDataForEngine(self, streamUuid: str) -> pd.DataFrame:
        """
        Get stream data formatted for engine consumption.
        Returns DataFrame with columns: date_time, value, id (matching engine format)

        Args:
            streamUuid: The stream UUID

        Returns:
            DataFrame with columns: date_time, value, id
        """
        df = self.db.getTableData(streamUuid)
        if df.empty:
            return pd.DataFrame(columns=['date_time', 'value', 'id'])

        # Convert from storage format to engine format
        df = df.reset_index()
        df = df.rename(columns={'ts': 'date_time', 'hash': 'id'})
        if 'provider' in df.columns:
            df = df.drop(columns=['provider'])
        return df

    def hasStreamData(self, streamUuid: str) -> bool:
        """Check if we have local data for a stream."""
        return self.db.getRowCount(streamUuid) > 0

    def getStreamRowCount(self, streamUuid: str) -> int:
        """Get number of rows for a stream."""
        return self.db.getRowCount(streamUuid)

    def getLatestStreamTimestamp(self, streamUuid: str) -> Optional[str]:
        """Get the latest timestamp for a stream."""
        return self.db.getLatestTimestamp(streamUuid)

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

    def storePredictionDf(self, predictionStreamUuid: str, df: pd.DataFrame) -> int:
        """
        Store predictions from DataFrame.

        Args:
            predictionStreamUuid: The prediction stream UUID
            df: DataFrame with 'value' column, index as timestamp

        Returns:
            Number of rows inserted
        """
        return self.db.insertDataframe(predictionStreamUuid, df, provider='engine')

    def getPredictions(self, predictionStreamUuid: str) -> pd.DataFrame:
        """Get all predictions for a prediction stream."""
        return self.db.getTableData(predictionStreamUuid)

    def getLatestPrediction(self, predictionStreamUuid: str) -> Optional[dict]:
        """
        Get the latest prediction for a stream.

        Returns:
            Dict with keys: ts, value, hash, provider or None
        """
        df = self.db.getTableData(predictionStreamUuid)
        if df.empty:
            return None
        last_row = df.iloc[-1]
        return {
            'ts': df.index[-1],
            'value': last_row['value'],
            'hash': last_row['hash'],
            'provider': last_row['provider']
        }

    # ==================== Utility Methods ====================

    def mergeServerDataWithLocal(
        self,
        streamUuid: str,
        serverDf: pd.DataFrame,
        provider: str = 'central'
    ) -> pd.DataFrame:
        """
        Merge server data with local data, preferring newer data.
        Returns the combined data formatted for engine use.

        Args:
            streamUuid: The stream UUID
            serverDf: DataFrame from server (should have 'value' column)
            provider: Data provider identifier

        Returns:
            Combined DataFrame formatted for engine (date_time, value, id)
        """
        # Store server data (will skip duplicates)
        if not serverDf.empty:
            self.storeStreamData(streamUuid, serverDf, provider)

        # Return combined data in engine format
        return self.getStreamDataForEngine(streamUuid)

    def listAllStreams(self) -> list:
        """List all stream UUIDs stored in the database."""
        return self.db.listTables()

    def deleteStreamData(self, streamUuid: str):
        """Delete all data for a stream."""
        self.db.deleteTable(streamUuid)

    def close(self):
        """Close database connection."""
        self.db.disconnect()
