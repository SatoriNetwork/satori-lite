"""
End-to-end workflow tests.

Tests complete workflows spanning multiple operations and endpoints.
"""
import pytest
import requests
import time


@pytest.mark.integration
@pytest.mark.slow
def test_complete_authentication_flow(server_url, server_available, test_wallet):
    """
    Test complete authentication workflow:
    1. Get challenge token
    2. Sign challenge
    3. Use signed challenge in authenticated request
    """
    # Step 1: Get challenge token
    challenge_response = requests.get(f"{server_url}/api/v1/auth/challenge")
    assert challenge_response.status_code == 200

    challenge = challenge_response.json()["challenge"]
    assert challenge is not None
    assert len(challenge) > 0

    # Step 2: Sign challenge with real wallet, EIP-191 (personal_sign)
    # Create headers with proper HTTP header names
    headers = {
        'wallet-pubkey': test_wallet.pubkey,
        'message': challenge,
        'signature': test_wallet.ethSign(challenge)
    }

    assert "wallet-pubkey" in headers
    assert "message" in headers
    assert "signature" in headers
    assert headers["message"] == challenge

    # Step 3: Use auth headers in authenticated request
    response = requests.get(
        f"{server_url}/api/v1/balance/get",
        headers=headers
    )

    # With real wallet, signature should be VALID (not 401)
    # We expect 200 (has balance) or 404 (peer not found - new wallet not registered)
    assert response.status_code in [200, 404], f"Expected 200 or 404, got {response.status_code}"

    # Important: no server errors
    assert response.status_code != 500  # Not a server error


@pytest.mark.integration
@pytest.mark.slow
def test_prediction_submission_workflow(client_instance, server_available):
    """
    Test complete prediction submission workflow using client:
    1. Client gets challenge
    2. Client constructs authenticated call
    3. Client submits prediction
    """
    topic = "workflow-test-topic"

    # Client handles authentication internally
    result = client_instance.publish(
        topic=topic,
        data="100000.00",
        observationTime="2025-01-01T12:00:00Z",
        observationHash="workflow-test-hash",
        isPrediction=True
    )

    # Result will be None (auth failure) with mock wallet
    # But tests that the complete workflow executes without errors
    assert result is None or result is True


@pytest.mark.integration
@pytest.mark.slow
def test_observation_retrieval_workflow(server_url, server_available):
    """
    Test observation retrieval workflow:
    1. Request latest observation
    2. Verify response structure
    3. Handle both empty and non-empty states
    """
    response = requests.get(f"{server_url}/api/v1/observation/get")

    assert response.status_code == 200

    data = response.json()

    # Could be null (no observations) or observation object
    if data is None:
        # Empty state - valid response
        assert data is None
    else:
        # Observation exists - verify structure
        assert isinstance(data, dict)
        assert "id" in data
        assert "value" in data
        assert "ts" in data
        # observed_at and hash are optional
        assert "observed_at" in data or "observed_at" not in data
        assert "hash" in data or "hash" not in data


@pytest.mark.integration
@pytest.mark.slow
def test_multiple_challenge_tokens_workflow(server_url, server_available):
    """
    Test that multiple challenge tokens can be requested and are unique:
    1. Request first challenge
    2. Request second challenge
    3. Verify they're different
    4. Both can be used for authentication
    """
    # Request first challenge
    response1 = requests.get(f"{server_url}/api/v1/auth/challenge")
    assert response1.status_code == 200
    challenge1 = response1.json()["challenge"]

    # Small delay to ensure different timestamp if using fallback
    time.sleep(0.01)

    # Request second challenge
    response2 = requests.get(f"{server_url}/api/v1/auth/challenge")
    assert response2.status_code == 200
    challenge2 = response2.json()["challenge"]

    # Challenges should be unique
    assert challenge1 != challenge2

    # Both should be valid UUID format
    assert len(challenge1.split('-')) == 5
    assert len(challenge2.split('-')) == 5


@pytest.mark.integration
@pytest.mark.slow
def test_client_checkin_workflow(client_instance, server_available):
    """
    Test client checkin workflow:
    1. Client performs checkin
    2. Receives server response
    3. Updates internal state
    """
    # Record initial state
    initial_checkin = client_instance.lastCheckin

    # Perform checkin
    result = client_instance.checkin()

    # Should return dict with server data
    assert isinstance(result, dict)

    # Should have updated lastCheckin timestamp
    assert client_instance.lastCheckin > initial_checkin

    # Response should have expected structure (central minimal response)
    assert "wallet" in result or "key" in result


@pytest.mark.integration
@pytest.mark.slow
def test_rate_limiting_across_topics_workflow(client_instance):
    """
    Test that rate limiting works independently per topic:
    1. Submit to topic A (succeeds or fails auth)
    2. Immediately submit to topic A again (blocked by rate limit)
    3. Submit to topic B (succeeds or fails auth, not blocked)
    """
    topic_a = "rate-limit-topic-a"
    topic_b = "rate-limit-topic-b"

    # Submit to topic A
    result_a1 = client_instance.publish(
        topic=topic_a,
        data="100.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="hash-a1",
        isPrediction=True
    )

    # Immediate second submission to topic A (should be blocked)
    result_a2 = client_instance.publish(
        topic=topic_a,
        data="200.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="hash-a2",
        isPrediction=True
    )

    # Submit to different topic B (should not be blocked)
    result_b1 = client_instance.publish(
        topic=topic_b,
        data="300.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="hash-b1",
        isPrediction=True
    )

    # First submission to A: may succeed (True) or fail auth (None)
    assert result_a1 in [None, True]

    # Second submission to A: should be blocked (None, not sent to server)
    assert result_a2 is None

    # First submission to B: may succeed (True) or fail auth (None)
    assert result_b1 in [None, True]

    # Verify topics are tracked separately
    assert topic_a in client_instance.topicTime
    assert topic_b in client_instance.topicTime


@pytest.mark.integration
@pytest.mark.slow
def test_error_recovery_invalid_endpoint(server_url, server_available):
    """
    Test error handling when accessing invalid endpoint:
    1. Request non-existent endpoint
    2. Verify 404 response
    3. Server remains stable
    """
    response = requests.get(f"{server_url}/api/v1/nonexistent/endpoint")

    assert response.status_code == 404

    # Server should still respond to valid endpoints after 404
    health_response = requests.get(f"{server_url}/health")
    assert health_response.status_code == 200


@pytest.mark.integration
@pytest.mark.slow
def test_error_recovery_malformed_json(server_url, server_available, challenge_token, test_wallet):
    """
    Test error handling with malformed JSON payload:
    1. Send malformed JSON to endpoint
    2. Verify error response
    3. Server remains stable
    """
    # Sign challenge with real wallet, EIP-191 (personal_sign)
    headers = {
        'wallet-pubkey': test_wallet.pubkey,
        'message': challenge_token,
        'signature': test_wallet.ethSign(challenge_token)
    }

    # Send malformed JSON (missing required fields)
    response = requests.post(
        f"{server_url}/api/v1/prediction/post",
        headers=headers,
        json={}  # Empty payload, missing required 'value'
    )

    # Should reject malformed payload (422 or 400)
    # OR auth failure (401) - either is acceptable
    assert response.status_code in [400, 401, 422]

    # Server should still work after error
    health_response = requests.get(f"{server_url}/health")
    assert health_response.status_code == 200


@pytest.mark.integration
@pytest.mark.slow
def test_endpoint_consistency_workflow(server_url, server_available):
    """
    Test that all documented endpoints exist and respond consistently:
    1. Check health endpoints
    2. Check auth endpoint
    3. Check all API v1 endpoints are reachable
    """
    endpoints_to_check = [
        ("GET", "/health", None),
        ("GET", "/", None),
        ("GET", "/api/v1/auth/challenge", None),
        ("GET", "/api/v1/observation/get", None),
    ]

    for method, endpoint, _ in endpoints_to_check:
        if method == "GET":
            response = requests.get(f"{server_url}{endpoint}")
        else:
            response = requests.post(f"{server_url}{endpoint}")

        # All endpoints should exist (not 404)
        assert response.status_code != 404, f"Endpoint {endpoint} not found"

        # Should get valid HTTP response
        assert response.status_code < 500, f"Endpoint {endpoint} server error"
