"""
Unit tests for SatoriServerClient methods.

Tests client methods in isolation with mocked HTTP requests.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import json
import time
import requests


@pytest.mark.unit
def test_publish_constructs_correct_prediction_payload(client_instance, mock_response):
    """Test publish() constructs correct payload for predictions."""
    with patch('requests.post', return_value=mock_response) as mock_post:
        client_instance.publish(
            topic="test-topic",
            data="100000.50",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="abc123",
            isPrediction=True
        )

    # Verify the endpoint and payload
    call_args = mock_post.call_args
    url = call_args[0][0]
    assert "/api/v1/prediction/post" in url

    # Check payload (it's a JSON string in the json kwarg)
    call_kwargs = call_args[1]
    payload_str = call_kwargs.get('json')
    assert payload_str is not None

    # Payload is a JSON string, parse it
    assert isinstance(payload_str, str)
    payload = json.loads(payload_str)
    assert payload['value'] == "100000.50"
    assert payload['observed_at'] == "2025-01-01T00:00:00Z"
    assert payload['hash'] == "abc123"


@pytest.mark.unit
def test_publish_returns_true_on_success(client_instance, mock_response):
    """Test publish() returns True on successful submission."""
    mock_response.status_code = 200

    with patch('requests.post', return_value=mock_response):
        result = client_instance.publish(
            topic="test-topic",
            data="12345",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash",
            isPrediction=True
        )

    assert result is True


@pytest.mark.unit
def test_publish_returns_none_on_error(client_instance, mock_response):
    """Test publish() returns None on HTTP error (status >= 400)."""
    mock_response.status_code = 401

    with patch('requests.post', return_value=mock_response):
        result = client_instance.publish(
            topic="test-topic",
            data="12345",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash",
            isPrediction=True
        )

    assert result is None


@pytest.mark.unit
def test_publish_returns_true_on_200_regardless_of_text(client_instance, mock_response):
    """Test publish() returns True on 200 status (even with 'fail' text).

    The implementation prioritizes status code over text content.
    """
    mock_response.status_code = 200
    mock_response.text = "fail"

    with patch('requests.post', return_value=mock_response):
        result = client_instance.publish(
            topic="test-topic",
            data="12345",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash",
            isPrediction=True
        )

    # When status is 200, returns True regardless of text
    assert result is True


@pytest.mark.unit
def test_publish_rate_limiting_prevents_duplicate_calls(client_instance, mock_response):
    """Test publish() rate limiting prevents too-frequent submissions."""
    topic = "rate-limited-topic"

    with patch('requests.post', return_value=mock_response) as mock_post:
        # First call should succeed
        result1 = client_instance.publish(
            topic=topic,
            data="100",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash1",
            isPrediction=True
        )

        # Immediate second call should be blocked by rate limiting
        result2 = client_instance.publish(
            topic=topic,
            data="200",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash2",
            isPrediction=True
        )

    # First call succeeds
    assert result1 is True

    # Second call blocked (returns None due to rate limiting)
    assert result2 is None

    # Only one HTTP call should have been made
    assert mock_post.call_count == 1


@pytest.mark.unit
def test_publish_different_topics_not_rate_limited(client_instance, mock_response):
    """Test publish() allows calls to different topics without rate limiting."""
    topic1 = "topic-one"
    topic2 = "topic-two"

    with patch('requests.post', return_value=mock_response) as mock_post:
        # First call to topic1
        result1 = client_instance.publish(
            topic=topic1,
            data="100",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash1",
            isPrediction=True
        )

        # Immediate call to topic2 should succeed (different topic)
        result2 = client_instance.publish(
            topic=topic2,
            data="200",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash2",
            isPrediction=True
        )

    assert result1 is True
    assert result2 is True
    # Both calls should succeed (different topics have separate rate limits)
    assert mock_post.call_count == 2


@pytest.mark.unit
def test_publish_observation_not_supported(client_instance, mock_response):
    """Test publish() returns None for observations (not yet supported)."""
    with patch('requests.post', return_value=mock_response) as mock_post:
        result = client_instance.publish(
            topic="test-topic",
            data="12345",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash",
            isPrediction=False  # Observation, not prediction
        )

    # Should return None (not supported)
    assert result is None

    # Should not make HTTP call
    mock_post.assert_not_called()


@pytest.mark.unit
def test_publish_exception_handling(client_instance):
    """Test publish() handles exceptions gracefully."""
    with patch('requests.post', side_effect=requests.exceptions.ConnectionError("Network error")):
        result = client_instance.publish(
            topic="test-topic",
            data="12345",
            observationTime="2025-01-01T00:00:00Z",
            observationHash="hash",
            isPrediction=True
        )

    # Should return None on exception
    assert result is None


@pytest.mark.unit
def test_checkin_calls_health_endpoint(client_instance, mock_response):
    """Test checkin() attempts to call health endpoint first."""
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "healthy"}

    with patch('requests.get', return_value=mock_response) as mock_get:
        with patch('requests.post', return_value=mock_response):
            result = client_instance.checkin()

    # Should call /health endpoint
    health_calls = [call for call in mock_get.call_args_list if '/health' in str(call)]
    assert len(health_calls) > 0


@pytest.mark.unit
def test_checkin_returns_minimal_data_for_central_lite(client_instance, mock_response):
    """Test checkin() returns minimal data when connecting to central."""
    mock_response.status_code = 200

    with patch('requests.get', return_value=mock_response):
        with patch('requests.post', return_value=mock_response):
            result = client_instance.checkin()

    # Should return dict with expected fields
    assert isinstance(result, dict)
    assert 'key' in result
    assert 'wallet' in result


@pytest.mark.unit
def test_checkin_updates_last_checkin_time(client_instance, mock_response):
    """Test checkin() updates lastCheckin timestamp."""
    mock_response.status_code = 200

    initial_time = client_instance.lastCheckin

    with patch('requests.get', return_value=mock_response):
        with patch('requests.post', return_value=mock_response):
            with patch('time.time', return_value=9999999.0):
                client_instance.checkin()

    # lastCheckin should be updated
    assert client_instance.lastCheckin == 9999999.0
    assert client_instance.lastCheckin != initial_time


@pytest.mark.unit
def test_set_topic_time(client_instance):
    """Test setTopicTime() records current time for topic."""
    topic = "test-topic"

    with patch('time.time', return_value=12345.0):
        client_instance.setTopicTime(topic)

    assert topic in client_instance.topicTime
    assert client_instance.topicTime[topic] == 12345.0


@pytest.mark.unit
def test_multiple_topics_tracked_separately(client_instance):
    """Test that different topics have separate rate limiting."""
    topic1 = "topic-one"
    topic2 = "topic-two"

    with patch('time.time', return_value=10000.0):
        client_instance.setTopicTime(topic1)

    with patch('time.time', return_value=20000.0):
        client_instance.setTopicTime(topic2)

    assert client_instance.topicTime[topic1] == 10000.0
    assert client_instance.topicTime[topic2] == 20000.0


@pytest.mark.unit
def test_client_initialization(test_alias, test_server_url):
    """Test SatoriServerClient initializes correctly."""
    from satorilib.server.server import SatoriServerClient

    client = SatoriServerClient(
        alias=test_alias,
        url=test_server_url
    )

    assert client.alias == test_alias
    assert client.url == test_server_url
    assert isinstance(client.topicTime, dict)
    assert client.lastCheckin == 0


@pytest.mark.unit
def test_client_uses_environment_variable_for_url(test_alias):
    """Test client uses SATORI_CENTRAL_URL environment variable."""
    from satorilib.server.server import SatoriServerClient

    with patch.dict('os.environ', {'SATORI_CENTRAL_URL': 'http://custom-server:9000'}):
        client = SatoriServerClient(alias=test_alias)

        assert client.url == 'http://custom-server:9000'


@pytest.mark.unit
def test_client_defaults_to_localhost(test_alias):
    """Test client defaults to localhost when no env var set."""
    from satorilib.server.server import SatoriServerClient

    with patch.dict('os.environ', {}, clear=True):
        # Remove SATORI_CENTRAL_URL if it exists
        client = SatoriServerClient(alias=test_alias)

        assert 'localhost:8000' in client.url or client.url == 'http://localhost:8000'


@pytest.mark.unit
def test_get_observation_returns_data_on_success(client_instance, mock_response):
    """Test getObservation() returns observation data on successful request."""
    observation_data = {
        "id": 123,
        "value": "42000.50",
        "observed_at": "2025-01-01T00:00:00Z",
        "hash": "abc123",
        "ts": "2025-01-01T00:00:00Z"
    }
    mock_response.status_code = 200
    mock_response.json.return_value = observation_data

    with patch('requests.get', return_value=mock_response) as mock_get:
        result = client_instance.getObservation()

    # Verify endpoint called
    call_args = mock_get.call_args
    url = call_args[0][0]
    assert "/api/v1/observation/get" in url

    # Verify data returned
    assert result == observation_data
    assert result['id'] == 123
    assert result['value'] == "42000.50"


@pytest.mark.unit
def test_get_observation_returns_none_when_no_observations(client_instance, mock_response):
    """Test getObservation() returns None when no observations available."""
    mock_response.status_code = 200
    mock_response.json.return_value = None

    with patch('requests.get', return_value=mock_response):
        result = client_instance.getObservation()

    assert result is None


@pytest.mark.unit
def test_get_observation_returns_none_on_error(client_instance, mock_response):
    """Test getObservation() returns None on HTTP error."""
    mock_response.status_code = 404

    with patch('requests.get', return_value=mock_response):
        result = client_instance.getObservation()

    assert result is None


@pytest.mark.unit
def test_get_observation_handles_exceptions(client_instance):
    """Test getObservation() handles exceptions gracefully."""
    with patch('requests.get', side_effect=requests.exceptions.ConnectionError("Network error")):
        result = client_instance.getObservation()

    # Should return None on exception
    assert result is None
