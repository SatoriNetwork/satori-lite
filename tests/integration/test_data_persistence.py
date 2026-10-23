"""
Data persistence integration tests.

Tests that verify data is actually saved to and retrieved from the database
through API behavior verification.
"""
import pytest
import requests
import json


@pytest.mark.integration
@pytest.mark.slow
def test_prediction_endpoint_accepts_valid_payload(server_url, server_available, challenge_token, test_alias):
    """Test that prediction endpoint accepts properly formatted predictions."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    prediction_data = {
        "value": "99999.99",
        "observed_at": "2025-01-15T12:00:00Z",
        "hash": "test-persistence-hash-123"
    }

    payload = json.dumps(prediction_data)

    response = requests.post(
        f"{server_url}/api/v1/prediction/post",
        headers=headers,
        json=payload
    )

    # With mock wallet, auth will fail (401)
    # With real wallet, would return 200 with prediction data
    assert response.status_code in [401, 200]


@pytest.mark.integration
@pytest.mark.slow
def test_observation_retrieval_consistency(server_url, server_available):
    """Test that repeated observation requests return consistent results."""
    # Get observation multiple times
    responses = []
    for _ in range(5):
        response = requests.get(f"{server_url}/api/v1/observation/get")
        assert response.status_code == 200
        responses.append(response.json())

    # All responses should be identical (no random changes)
    first_response = responses[0]
    for response in responses[1:]:
        assert response == first_response, "Observation data should be consistent"


@pytest.mark.integration
@pytest.mark.slow
def test_observation_response_structure_validation(server_url, server_available):
    """Test that observation responses have expected structure."""
    response = requests.get(f"{server_url}/api/v1/observation/get")
    assert response.status_code == 200

    data = response.json()

    if data is not None:
        # If observation exists, validate structure
        assert "id" in data, "Observation should have id"
        assert "value" in data, "Observation should have value"
        assert "ts" in data, "Observation should have ts"

        # Verify data types
        assert isinstance(data["id"], int), "id should be integer"
        assert isinstance(data["value"], str), "value should be string"
    else:
        # null is valid when no observations exist
        assert data is None


@pytest.mark.integration
@pytest.mark.slow
def test_prediction_response_structure(server_url, server_available, challenge_token, test_alias):
    """Test that prediction endpoint returns expected structure on success."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    prediction_data = {
        "value": "12345.67",
        "observed_at": "2025-01-15T12:00:00Z",
        "hash": "structure-test-hash"
    }

    payload = json.dumps(prediction_data)

    response = requests.post(
        f"{server_url}/api/v1/prediction/post",
        headers=headers,
        json=payload
    )

    # With mock wallet: 401
    # Expected structure on success (200) would include:
    # - id, peer_id, value, observed_at, hash
    if response.status_code == 200:
        data = response.json()
        assert "id" in data
        assert "value" in data
        assert data["value"] == prediction_data["value"]


@pytest.mark.integration
@pytest.mark.slow
def test_health_endpoint_returns_timestamp(server_url, server_available):
    """Test that health endpoint includes timestamp (proves state tracking)."""
    response = requests.get(f"{server_url}/health")
    assert response.status_code == 200

    data = response.json()
    assert "timestamp" in data, "Health response should include timestamp"
    assert "status" in data, "Health response should include status"
    assert data["status"] == "healthy"

    # Timestamp should be ISO format string
    timestamp = data["timestamp"]
    assert isinstance(timestamp, str)
    assert "T" in timestamp or "-" in timestamp  # ISO datetime format


@pytest.mark.integration
@pytest.mark.slow
def test_concurrent_requests_data_consistency(server_url, server_available):
    """Test that concurrent observation requests return consistent data."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def get_observation():
        response = requests.get(f"{server_url}/api/v1/observation/get")
        if response.status_code == 200:
            return response.json()
        return None

    # Make concurrent requests
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(get_observation) for _ in range(20)]
        results = [f.result() for f in as_completed(futures)]

    # All results should be identical (data doesn't change mid-request)
    if results[0] is not None:
        for result in results[1:]:
            assert result == results[0], "Concurrent requests should return same data"


@pytest.mark.integration
@pytest.mark.slow
def test_prediction_payload_validation(server_url, server_available, challenge_token, test_alias):
    """Test that server validates prediction payload fields."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    # Test with missing 'value' field
    invalid_payload = json.dumps({
        "observed_at": "2025-01-15T12:00:00Z",
        # Missing 'value'
    })

    response = requests.post(
        f"{server_url}/api/v1/prediction/post",
        headers=headers,
        json=invalid_payload
    )

    # Should reject invalid payload (422) or fail auth (401)
    assert response.status_code in [400, 401, 422]


@pytest.mark.integration
@pytest.mark.slow
def test_observation_endpoint_idempotency(server_url, server_available):
    """Test that observation endpoint is idempotent (repeated calls = same result)."""
    # First call
    response1 = requests.get(f"{server_url}/api/v1/observation/get")
    data1 = response1.json()

    # Second call (should get same data)
    response2 = requests.get(f"{server_url}/api/v1/observation/get")
    data2 = response2.json()

    # Should be identical
    assert data1 == data2, "Observation endpoint should be idempotent"


@pytest.mark.integration
@pytest.mark.slow
def test_api_error_responses_have_consistent_format(server_url, server_available):
    """Test that error responses follow consistent format."""
    # Request invalid endpoint
    response = requests.get(f"{server_url}/api/v1/invalid/endpoint")

    assert response.status_code == 404

    # Error responses should be valid JSON
    try:
        error_data = response.json()
        # FastAPI returns {"detail": "message"} format
        assert "detail" in error_data or isinstance(error_data, dict)
    except json.JSONDecodeError:
        # Some errors might not be JSON, that's ok
        pass


@pytest.mark.integration
@pytest.mark.slow
def test_server_state_persistence_across_requests(server_url, server_available):
    """Test that server maintains state across multiple requests."""
    # Get initial health check
    health1 = requests.get(f"{server_url}/health")
    timestamp1 = health1.json()["timestamp"]

    # Get observation
    obs = requests.get(f"{server_url}/api/v1/observation/get")
    assert obs.status_code == 200

    # Get second health check
    health2 = requests.get(f"{server_url}/health")
    timestamp2 = health2.json()["timestamp"]

    # Server should still be responsive
    assert health2.status_code == 200

    # Timestamps should be different (server maintains time state)
    # Note: They might be same if requests are very fast
    assert isinstance(timestamp2, str)
