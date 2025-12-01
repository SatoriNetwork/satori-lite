"""
Integration Test Suite for Satori Lite

Tests:
1. Docker container startup and basic functionality
2. Neuron spawning the engine
3. Data flow from neuron to engine
4. Mock central server data reception and SQLite storage

Run inside Docker:
    docker exec -it satori python /Satori/Neuron/../tests/test_integration.py
"""
import os
import sys
import json
import time
import threading
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch

# Add paths for imports
sys.path.insert(0, '/Satori/Lib')
sys.path.insert(0, '/Satori/Neuron')
sys.path.insert(0, '/Satori/Engine')

from satorilib.concepts.structs import StreamId, Stream
from satorilib.server import SatoriServerClient
from satoriengine.veda.engine import Engine, StreamModel
from satoriengine.veda.storage import EngineStorageManager


def print_header(title: str):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_result(test_name: str, passed: bool, message: str = ""):
    status = "✓ PASS" if passed else "✗ FAIL"
    print(f"  {status}: {test_name}")
    if message:
        print(f"         {message}")


class MockServerClient:
    """Mock Central Server for testing"""

    def __init__(self):
        self.published_predictions = []
        self.observations = []

    def publish(self, topic, data, observationTime, observationHash, isPrediction=False, useAuthorizedCall=True):
        self.published_predictions.append({
            'topic': topic,
            'data': data,
            'time': observationTime,
            'hash': observationHash
        })
        return True

    def getObservations(self, streamId: str) -> list:
        return self.observations

    def addObservation(self, ts, value, hash_val):
        self.observations.append({
            'ts': ts,
            'value': value,
            'hash': hash_val
        })


class MockWallet:
    """Mock wallet for testing"""
    def __init__(self):
        self.address = "ETestAddress123456789012345678901234"
        self.pubkey = "TestPubKey123"


def create_mock_stream(source: str = "test", author: str = "author", stream: str = "stream", target: str = "") -> Stream:
    """Create a mock Stream object"""
    streamId = StreamId(source=source, author=author, stream=stream, target=target)
    return Stream(streamId=streamId)


def create_mock_subscription_publication_pair():
    """Create matching subscription and publication streams"""
    # StreamId takes: source, author, stream, target
    sub_stream_id = StreamId(source="test", author="test_author", stream="data_stream", target="")
    pub_stream_id = StreamId(source="test", author="test_author", stream="prediction_stream", target="")

    subscription = Stream(streamId=sub_stream_id)
    publication = Stream(streamId=pub_stream_id, predicting=sub_stream_id)

    return subscription, publication


# =============================================================================
# TEST 1: Container Startup and Basic Functionality
# =============================================================================
def test_container_startup():
    """Test that the container environment is properly set up"""
    print_header("TEST 1: Container Startup and Basic Functionality")

    all_passed = True

    # Check Python path setup
    try:
        from satorilib.concepts import StreamId
        print_result("Import satorilib", True)
    except ImportError as e:
        print_result("Import satorilib", False, str(e))
        all_passed = False

    try:
        from satoriengine.veda.engine import Engine
        print_result("Import satoriengine", True)
    except ImportError as e:
        print_result("Import satoriengine", False, str(e))
        all_passed = False

    try:
        from satorineuron.structs.start import RunMode
        print_result("Import satorineuron", True)
    except ImportError as e:
        print_result("Import satorineuron", False, str(e))
        all_passed = False

    # Check directory structure
    dirs_to_check = [
        '/Satori/Lib',
        '/Satori/Neuron',
        '/Satori/Engine'
    ]
    for d in dirs_to_check:
        exists = os.path.isdir(d)
        print_result(f"Directory exists: {d}", exists)
        if not exists:
            all_passed = False

    # Check that we can create storage directory
    test_dir = '/tmp/satori_test'
    os.makedirs(test_dir, exist_ok=True)
    print_result("Can create temp directories", os.path.isdir(test_dir))

    return all_passed


# =============================================================================
# TEST 2: Neuron Spawning the Engine
# =============================================================================
def test_neuron_spawns_engine():
    """Test that neuron can spawn the engine with stream assignments"""
    print_header("TEST 2: Neuron Spawning the Engine")

    all_passed = True

    # Create mock data
    subscription, publication = create_mock_subscription_publication_pair()
    mock_server = MockServerClient()
    mock_wallet = MockWallet()

    # Test Engine.createFromNeuron()
    try:
        engine = Engine.createFromNeuron(
            subscriptions=[subscription],
            publications=[publication],
            server=mock_server,
            wallet=mock_wallet
        )
        print_result("Engine.createFromNeuron()", True)
    except Exception as e:
        print_result("Engine.createFromNeuron()", False, str(e))
        return False

    # Verify engine has correct attributes
    has_server = engine.server is mock_server
    print_result("Engine has server reference", has_server)
    if not has_server:
        all_passed = False

    has_wallet = engine.wallet is mock_wallet
    print_result("Engine has wallet reference", has_wallet)
    if not has_wallet:
        all_passed = False

    has_subs = len(engine.subscriptionStreams) == 1
    print_result("Engine has subscription streams", has_subs, f"Count: {len(engine.subscriptionStreams)}")
    if not has_subs:
        all_passed = False

    has_pubs = len(engine.publicationStreams) == 1
    print_result("Engine has publication streams", has_pubs, f"Count: {len(engine.publicationStreams)}")
    if not has_pubs:
        all_passed = False

    # Test storage manager initialization
    has_storage = engine.storage is not None
    print_result("Engine has storage manager", has_storage)
    if not has_storage:
        all_passed = False

    return all_passed


# =============================================================================
# TEST 3: Data Flow from Neuron to Engine
# =============================================================================
def test_data_flow_neuron_to_engine():
    """Test data flow from neuron stream assignments to engine models"""
    print_header("TEST 3: Data Flow from Neuron to Engine")

    all_passed = True

    # Use a fresh temp storage
    test_dir = '/tmp/satori_flow_test'
    os.makedirs(test_dir, exist_ok=True)

    # Reset storage singleton
    EngineStorageManager._instance = None
    storage = EngineStorageManager(data_dir=test_dir, dbname='flow_test.db')

    # Create mock components
    subscription, publication = create_mock_subscription_publication_pair()
    mock_server = MockServerClient()
    mock_wallet = MockWallet()

    # Create engine
    engine = Engine.createFromNeuron(
        subscriptions=[subscription],
        publications=[publication],
        server=mock_server,
        wallet=mock_wallet
    )
    engine.storage = storage

    # Test StreamModel.createFromServer()
    try:
        stream_model = StreamModel.createFromServer(
            streamUuid=subscription.streamId.uuid,
            predictionStreamUuid=publication.streamId.uuid,
            server=mock_server,
            wallet=mock_wallet,
            subscriptionStream=subscription,
            publicationStream=publication,
            pauseAll=lambda: None,
            resumeAll=lambda: None,
            storage=storage
        )
        print_result("StreamModel.createFromServer()", True)
    except Exception as e:
        print_result("StreamModel.createFromServer()", False, str(e))
        import traceback
        traceback.print_exc()
        return False

    # Verify stream model attributes
    has_uuid = stream_model.streamUuid == subscription.streamId.uuid
    print_result("StreamModel has correct subscription UUID", has_uuid)
    if not has_uuid:
        all_passed = False

    has_pred_uuid = stream_model.predictionStreamUuid == publication.streamId.uuid
    print_result("StreamModel has correct publication UUID", has_pred_uuid)
    if not has_pred_uuid:
        all_passed = False

    uses_server = stream_model.useServer == True
    print_result("StreamModel uses Central Server mode", uses_server)
    if not uses_server:
        all_passed = False

    has_storage = stream_model.storage is not None
    print_result("StreamModel has storage reference", has_storage)
    if not has_storage:
        all_passed = False

    # Clean up
    storage.close()

    return all_passed


# =============================================================================
# TEST 4: Mock Central Server Data to SQLite Storage
# =============================================================================
def test_mock_server_data_to_sqlite():
    """Test receiving mock data from central server and storing in SQLite"""
    print_header("TEST 4: Mock Central Server Data to SQLite Storage")

    all_passed = True

    # Use a fresh temp storage
    test_dir = '/tmp/satori_sqlite_test'
    os.makedirs(test_dir, exist_ok=True)

    # Reset storage singleton
    EngineStorageManager._instance = None
    storage = EngineStorageManager(data_dir=test_dir, dbname='sqlite_test.db')

    # Create mock components
    subscription, publication = create_mock_subscription_publication_pair()
    mock_server = MockServerClient()
    mock_wallet = MockWallet()

    # Create stream model
    stream_model = StreamModel.createFromServer(
        streamUuid=subscription.streamId.uuid,
        predictionStreamUuid=publication.streamId.uuid,
        server=mock_server,
        wallet=mock_wallet,
        subscriptionStream=subscription,
        publicationStream=publication,
        pauseAll=lambda: None,
        resumeAll=lambda: None,
        storage=storage
    )

    # Simulate receiving data from Central Server
    mock_data = pd.DataFrame({
        'ts': [
            '2024-01-01T00:00:00',
            '2024-01-01T01:00:00',
            '2024-01-01T02:00:00'
        ],
        'value': [100.0, 101.5, 102.3],
        'hash': ['hash1', 'hash2', 'hash3']
    })

    # Test onDataReceived method
    try:
        stream_model.onDataReceived(mock_data)
        print_result("StreamModel.onDataReceived()", True)
    except Exception as e:
        print_result("StreamModel.onDataReceived()", False, str(e))
        import traceback
        traceback.print_exc()
        all_passed = False

    # Verify data was stored in SQLite
    stored_data = storage.getStreamDataForEngine(subscription.streamId.uuid)

    data_stored = len(stored_data) == 3
    print_result("Data stored in SQLite", data_stored, f"Rows: {len(stored_data)}")
    if not data_stored:
        all_passed = False

    # Verify data integrity
    if len(stored_data) > 0:
        has_date_time = 'date_time' in stored_data.columns
        print_result("SQLite data has date_time column", has_date_time)
        if not has_date_time:
            all_passed = False

        has_value = 'value' in stored_data.columns
        print_result("SQLite data has value column", has_value)
        if not has_value:
            all_passed = False

        has_id = 'id' in stored_data.columns
        print_result("SQLite data has id column", has_id)
        if not has_id:
            all_passed = False

        # Check values
        values_match = list(stored_data['value']) == [100.0, 101.5, 102.3]
        print_result("Data values match", values_match)
        if not values_match:
            all_passed = False

    # Verify in-memory data was also updated
    in_memory_count = len(stream_model.data)
    in_memory_updated = in_memory_count == 3
    print_result("In-memory data updated", in_memory_updated, f"Rows: {in_memory_count}")
    if not in_memory_updated:
        all_passed = False

    # Test duplicate prevention
    stream_model.onDataReceived(mock_data)  # Send same data again
    stored_after_dup = storage.getStreamDataForEngine(subscription.streamId.uuid)
    no_duplicates = len(stored_after_dup) == 3
    print_result("Duplicate prevention works", no_duplicates, f"Rows after duplicate: {len(stored_after_dup)}")
    if not no_duplicates:
        all_passed = False

    # Test incremental data
    new_data = pd.DataFrame({
        'ts': ['2024-01-01T03:00:00'],
        'value': [103.0],
        'hash': ['hash4']
    })
    stream_model.onDataReceived(new_data)

    final_data = storage.getStreamDataForEngine(subscription.streamId.uuid)
    incremental_works = len(final_data) == 4
    print_result("Incremental data storage works", incremental_works, f"Final rows: {len(final_data)}")
    if not incremental_works:
        all_passed = False

    # Clean up
    storage.close()

    return all_passed


# =============================================================================
# TEST 5: Full Integration - Mock Server to Engine to Prediction
# =============================================================================
def test_full_integration():
    """Test full flow: mock server data → engine → prediction → publish"""
    print_header("TEST 5: Full Integration Flow")

    all_passed = True

    # Use a fresh temp storage
    test_dir = '/tmp/satori_full_test'
    os.makedirs(test_dir, exist_ok=True)

    # Reset storage singleton
    EngineStorageManager._instance = None
    storage = EngineStorageManager(data_dir=test_dir, dbname='full_test.db')

    # Create mock components
    subscription, publication = create_mock_subscription_publication_pair()
    mock_server = MockServerClient()
    mock_wallet = MockWallet()

    # Create stream model
    stream_model = StreamModel.createFromServer(
        streamUuid=subscription.streamId.uuid,
        predictionStreamUuid=publication.streamId.uuid,
        server=mock_server,
        wallet=mock_wallet,
        subscriptionStream=subscription,
        publicationStream=publication,
        pauseAll=lambda: None,
        resumeAll=lambda: None,
        storage=storage
    )

    # Add substantial training data
    base_time = datetime(2024, 1, 1)
    training_data = pd.DataFrame({
        'ts': [(base_time + timedelta(hours=i)).isoformat() for i in range(50)],
        'value': [100.0 + i * 0.5 + (i % 5) * 0.1 for i in range(50)],
        'hash': [f'hash_{i}' for i in range(50)]
    })

    stream_model.onDataReceived(training_data)

    data_received = len(stream_model.data) == 50
    print_result("Training data received", data_received, f"Rows: {len(stream_model.data)}")
    if not data_received:
        all_passed = False

    # Verify data stored in SQLite
    stored = storage.getStreamDataForEngine(subscription.streamId.uuid)
    data_persisted = len(stored) == 50
    print_result("Training data persisted to SQLite", data_persisted, f"Rows: {len(stored)}")
    if not data_persisted:
        all_passed = False

    # Test prediction publishing (simulated - we won't actually train the model)
    test_prediction = pd.DataFrame({
        'value': [125.5]
    }, index=[datetime.now().isoformat()])

    try:
        stream_model.publishPredictionToServer(test_prediction)
        print_result("publishPredictionToServer() executed", True)
    except Exception as e:
        print_result("publishPredictionToServer() executed", False, str(e))
        all_passed = False

    # Verify prediction was "sent" to mock server
    prediction_sent = len(mock_server.published_predictions) == 1
    print_result("Prediction sent to mock server", prediction_sent)
    if not prediction_sent:
        all_passed = False

    if prediction_sent:
        pred = mock_server.published_predictions[0]
        pred_value_correct = pred['data'] == '125.5'
        print_result("Prediction value correct", pred_value_correct, f"Value: {pred['data']}")
        if not pred_value_correct:
            all_passed = False

    # Verify prediction stored locally
    predictions = storage.getPredictions(publication.streamId.uuid)
    pred_stored = len(predictions) == 1
    print_result("Prediction stored in SQLite", pred_stored, f"Predictions: {len(predictions)}")
    if not pred_stored:
        all_passed = False

    # Clean up
    storage.close()

    return all_passed


# =============================================================================
# MAIN
# =============================================================================
def main():
    print("\n" + "=" * 60)
    print("  SATORI LITE INTEGRATION TEST SUITE")
    print("=" * 60)
    print(f"  Time: {datetime.now().isoformat()}")
    print("=" * 60)

    results = {}

    # Run all tests
    results['Container Startup'] = test_container_startup()
    results['Neuron Spawns Engine'] = test_neuron_spawns_engine()
    results['Data Flow Neuron to Engine'] = test_data_flow_neuron_to_engine()
    results['Mock Server to SQLite'] = test_mock_server_data_to_sqlite()
    results['Full Integration'] = test_full_integration()

    # Summary
    print_header("TEST SUMMARY")

    all_passed = True
    for test_name, passed in results.items():
        status = "✓ PASSED" if passed else "✗ FAILED"
        print(f"  {status}: {test_name}")
        if not passed:
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("  ALL TESTS PASSED!")
    else:
        print("  SOME TESTS FAILED")
    print("=" * 60 + "\n")

    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())
