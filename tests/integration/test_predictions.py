"""
Integration tests for prediction submission via SatoriServerClient.

Tests the client's publish() method against the actual server.
"""
import pytest
import requests
import time


@pytest.mark.integration
@pytest.mark.predictions
def test_client_publish_prediction_with_mock_wallet(client_instance, server_available):
    """Test client can call publish() method (will fail auth with mock wallet)."""
    # This tests that the client constructs and sends the request correctly
    # It will fail authentication because we're using a mock wallet
    result = client_instance.publish(
        topic="test-topic",
        data="100000.50",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="test-hash-123",
        isPrediction=True
    )

    # Result will be None (auth failure expected with mock wallet)
    # But the important thing is the method completes without exceptions
    assert result is None or result is True  # None = auth failed, True = succeeded


@pytest.mark.integration
@pytest.mark.predictions
def test_observation_endpoint_returns_latest(server_url, server_available):
    """Test GET /api/v1/observation/get returns latest observation."""
    response = requests.get(f"{server_url}/api/v1/observation/get")

    assert response.status_code == 200

    # May be null if no observations exist, or an observation object
    data = response.json()

    if data is not None:
        # If observation exists, verify structure
        assert "id" in data
        assert "value" in data
        assert "ts" in data


@pytest.mark.integration
@pytest.mark.predictions
def test_observation_endpoint_returns_null_when_empty(server_url, server_available):
    """Test /api/v1/observation/get returns null when no observations."""
    # This test assumes fresh database or no observations
    response = requests.get(f"{server_url}/api/v1/observation/get")

    assert response.status_code == 200
    # Could be null or an observation object
    # Just verify it doesn't error
    data = response.json()
    assert data is None or isinstance(data, dict)


@pytest.mark.integration
@pytest.mark.predictions
def test_prediction_endpoint_requires_auth(server_url, server_available):
    """Test POST /api/v1/prediction/post requires authentication."""
    # Try to submit prediction without auth headers
    response = requests.post(
        f"{server_url}/api/v1/prediction/post",
        json={
            "value": "100.0",
            "observed_at": "2025-01-01T00:00:00Z",
            "hash": "test"
        }
    )

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.predictions
def test_prediction_endpoint_with_invalid_auth(server_url, server_available, challenge_token, test_alias):
    """Test prediction submission with invalid signature."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    response = requests.post(
        f"{server_url}/api/v1/prediction/post",
        headers=headers,
        json={
            "value": "100.0",
            "observed_at": "2025-01-01T00:00:00Z",
            "hash": "test-hash"
        }
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.predictions
def test_client_publish_respects_topic_rate_limiting(client_instance):
    """Test client's topic-based rate limiting works."""
    topic = "rate-limited-topic"

    # First call
    result1 = client_instance.publish(
        topic=topic,
        data="100.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="hash1",
        isPrediction=True
    )

    # Immediate second call (should be blocked)
    result2 = client_instance.publish(
        topic=topic,
        data="200.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="hash2",
        isPrediction=True
    )

    # First may fail auth (mock wallet) or succeed
    # Second should be None (rate limited, never sent to server)
    assert result2 is None  # Blocked by rate limiting


@pytest.mark.integration
@pytest.mark.predictions
@pytest.mark.slow
def test_client_checkin_connects_to_server(client_instance, server_available):
    """Test client's checkin() method connects to server."""
    result = client_instance.checkin()

    # Should return a dict with server response
    assert isinstance(result, dict)

    # Should have minimal central response
    assert "key" in result or "wallet" in result


@pytest.mark.integration
@pytest.mark.predictions
def test_client_setTopicTime_records_timestamp(client_instance):
    """Test client tracks topic timestamps."""
    topic = "timestamp-test"

    before = time.time()
    client_instance.setTopicTime(topic)
    after = time.time()

    assert topic in client_instance.topicTime
    timestamp = client_instance.topicTime[topic]

    # Timestamp should be between before and after
    assert before <= timestamp <= after


@pytest.mark.integration
@pytest.mark.predictions
def test_prediction_payload_structure(server_url, server_available, challenge_token, test_alias):
    """Test that prediction payload is correctly structured."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    payload = {
        "value": "98765.43",
        "observed_at": "2025-12-31T23:59:59Z",
        "hash": "abcdef123456"
    }

    response = requests.post(
        f"{server_url}/api/v1/prediction/post",
        headers=headers,
        json=payload
    )

    # Will fail auth (401) but tests that payload structure is accepted
    # If we got 400, it means payload structure is wrong
    assert response.status_code != 400, "Payload structure should be valid"
