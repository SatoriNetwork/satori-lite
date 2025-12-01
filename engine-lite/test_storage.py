"""
Test script for engine SQLite storage.
Run this to verify the database is working correctly.

Run inside Docker:
    docker exec -it satori-neuron python /Satori/Engine/test_storage.py
"""
import os
import sys
import pandas as pd
from datetime import datetime, timedelta

# Imports work inside Docker where paths are already set up
from satoriengine.veda.storage.sqlite_manager import EngineSqliteDatabase
from satoriengine.veda.storage.manager import EngineStorageManager


def test_sqlite_manager():
    """Test the low-level SQLite manager."""
    print("\n=== Testing EngineSqliteDatabase ===")

    # Use temp directory for testing
    test_dir = '/tmp/satori_engine_test'
    os.makedirs(test_dir, exist_ok=True)

    db = EngineSqliteDatabase(data_dir=test_dir, dbname='test_engine.db')

    # Test table creation
    test_uuid = 'test-stream-uuid-12345'
    db.createTable(test_uuid)
    print(f"✓ Created table: {test_uuid}")

    # Test table exists
    assert db.tableExists(test_uuid), "Table should exist"
    print(f"✓ Table exists check passed")

    # Test insert row
    ts1 = '2024-01-01T00:00:00'
    inserted = db.insertRow(test_uuid, ts1, 100.5, 'hash1', 'test_provider')
    assert inserted, "Should insert row"
    print(f"✓ Inserted single row")

    # Test duplicate prevention
    inserted_dup = db.insertRow(test_uuid, ts1, 100.5, 'hash1', 'test_provider')
    assert not inserted_dup, "Should not insert duplicate"
    print(f"✓ Duplicate prevention works")

    # Test insert DataFrame
    test_df = pd.DataFrame({
        'value': [101.0, 102.0, 103.0],
        'hash': ['h2', 'h3', 'h4'],
        'provider': ['test', 'test', 'test']
    }, index=['2024-01-01T01:00:00', '2024-01-01T02:00:00', '2024-01-01T03:00:00'])

    count = db.insertDataframe(test_uuid, test_df, provider='test')
    assert count == 3, f"Should insert 3 rows, got {count}"
    print(f"✓ Inserted DataFrame with {count} rows")

    # Test get data
    result_df = db.getTableData(test_uuid)
    assert len(result_df) == 4, f"Should have 4 rows, got {len(result_df)}"
    print(f"✓ Retrieved {len(result_df)} rows from table")

    # Test get row count
    row_count = db.getRowCount(test_uuid)
    assert row_count == 4, f"Row count should be 4, got {row_count}"
    print(f"✓ Row count: {row_count}")

    # Test get last hash
    last_hash = db.getLastHash(test_uuid)
    assert last_hash is not None, "Should have last hash"
    print(f"✓ Last hash: {last_hash}")

    # Test get latest timestamp
    latest_ts = db.getLatestTimestamp(test_uuid)
    assert latest_ts == '2024-01-01T03:00:00', f"Latest ts should be '2024-01-01T03:00:00', got {latest_ts}"
    print(f"✓ Latest timestamp: {latest_ts}")

    # Test list tables
    tables = db.listTables()
    assert test_uuid in tables, "Test table should be in list"
    print(f"✓ Listed tables: {tables}")

    # Test delete table
    db.deleteTable(test_uuid)
    assert not db.tableExists(test_uuid), "Table should be deleted"
    print(f"✓ Deleted table")

    db.disconnect()
    print("\n✓ All EngineSqliteDatabase tests passed!\n")


def test_storage_manager():
    """Test the high-level storage manager."""
    print("\n=== Testing EngineStorageManager ===")

    # Use temp directory for testing
    test_dir = '/tmp/satori_engine_test'
    os.makedirs(test_dir, exist_ok=True)

    # Reset singleton for testing
    EngineStorageManager._instance = None
    storage = EngineStorageManager(data_dir=test_dir, dbname='test_manager.db')

    stream_uuid = 'stream-abc-123'
    prediction_uuid = 'pred-xyz-789'

    # Test store stream data
    stream_df = pd.DataFrame({
        'value': [50.0, 51.0, 52.0],
        'hash': ['sh1', 'sh2', 'sh3'],
    }, index=['2024-02-01T00:00:00', '2024-02-01T01:00:00', '2024-02-01T02:00:00'])

    inserted = storage.storeStreamData(stream_uuid, stream_df, provider='central')
    assert inserted == 3, f"Should insert 3 rows, got {inserted}"
    print(f"✓ Stored stream data: {inserted} rows")

    # Test has stream data
    assert storage.hasStreamData(stream_uuid), "Should have stream data"
    print(f"✓ hasStreamData check passed")

    # Test get stream data for engine
    engine_df = storage.getStreamDataForEngine(stream_uuid)
    assert 'date_time' in engine_df.columns, "Should have date_time column"
    assert 'value' in engine_df.columns, "Should have value column"
    assert 'id' in engine_df.columns, "Should have id column"
    assert len(engine_df) == 3, f"Should have 3 rows, got {len(engine_df)}"
    print(f"✓ Got stream data for engine: {len(engine_df)} rows")
    print(f"  Columns: {list(engine_df.columns)}")

    # Test store single observation
    stored = storage.storeStreamObservation(
        stream_uuid,
        '2024-02-01T03:00:00',
        53.0,
        'sh4',
        'central'
    )
    assert stored, "Should store observation"
    print(f"✓ Stored single observation")

    # Test get stream row count
    count = storage.getStreamRowCount(stream_uuid)
    assert count == 4, f"Should have 4 rows, got {count}"
    print(f"✓ Stream row count: {count}")

    # Test store prediction
    stored = storage.storePrediction(
        prediction_uuid,
        '2024-02-01T04:00:00',
        55.5,
        'pred_hash_1',
        'engine'
    )
    assert stored, "Should store prediction"
    print(f"✓ Stored prediction")

    # Test store prediction DataFrame
    pred_df = pd.DataFrame({
        'value': [56.0, 57.0],
    }, index=['2024-02-01T05:00:00', '2024-02-01T06:00:00'])
    inserted = storage.storePredictionDf(prediction_uuid, pred_df)
    assert inserted == 2, f"Should insert 2 predictions, got {inserted}"
    print(f"✓ Stored prediction DataFrame: {inserted} rows")

    # Test get predictions
    predictions_df = storage.getPredictions(prediction_uuid)
    assert len(predictions_df) == 3, f"Should have 3 predictions, got {len(predictions_df)}"
    print(f"✓ Got predictions: {len(predictions_df)} rows")

    # Test get latest prediction
    latest = storage.getLatestPrediction(prediction_uuid)
    assert latest is not None, "Should have latest prediction"
    assert float(latest['value']) == 57.0, f"Latest value should be 57.0, got {latest['value']}"
    print(f"✓ Latest prediction: {latest}")

    # Test list all streams
    streams = storage.listAllStreams()
    assert stream_uuid in streams, "Stream should be in list"
    assert prediction_uuid in streams, "Prediction stream should be in list"
    print(f"✓ Listed all streams: {streams}")

    # Cleanup
    storage.deleteStreamData(stream_uuid)
    storage.deleteStreamData(prediction_uuid)
    storage.close()

    print("\n✓ All EngineStorageManager tests passed!\n")


def main():
    print("=" * 60)
    print("Engine SQLite Storage Test Suite")
    print("=" * 60)

    try:
        test_sqlite_manager()
        test_storage_manager()
        print("=" * 60)
        print("ALL TESTS PASSED!")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
