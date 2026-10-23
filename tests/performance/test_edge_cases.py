"""
Edge case and data validation tests for neuron client and server.

Tests boundary conditions, special characters, large values, and data validation.
"""
import pytest
import requests
import json


@pytest.mark.slow
@pytest.mark.integration
def test_large_prediction_value(client_instance, server_available):
    """Test that extremely large prediction values are handled correctly."""
    # Test with very large float value
    large_value = "999999999999999999.99"

    result = client_instance.publish(
        topic="large-value-test",
        data=large_value,
        observationTime="2025-01-01T00:00:00Z",
        observationHash="large-hash",
        isPrediction=True
    )

    # Should either succeed or fail auth, but not crash
    assert result in [None, True]


@pytest.mark.slow
@pytest.mark.integration
def test_negative_prediction_value(client_instance, server_available):
    """Test that negative prediction values are handled."""
    result = client_instance.publish(
        topic="negative-value-test",
        data="-12345.67",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="negative-hash",
        isPrediction=True
    )

    # Should handle negative values
    assert result in [None, True]


@pytest.mark.slow
@pytest.mark.integration
def test_zero_prediction_value(client_instance, server_available):
    """Test edge case of zero value prediction."""
    result = client_instance.publish(
        topic="zero-value-test",
        data="0.00",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="zero-hash",
        isPrediction=True
    )

    # Zero should be valid
    assert result in [None, True]


@pytest.mark.slow
@pytest.mark.integration
def test_special_characters_in_topic(client_instance, server_available):
    """Test that topics with special characters are handled."""
    # Test various special characters that might appear in topics
    special_topics = [
        "topic-with-dashes",
        "topic_with_underscores",
        "topic.with.dots",
        "TOPIC-IN-CAPS",
        "topic123numbers",
    ]

    for topic in special_topics:
        result = client_instance.publish(
            topic=topic,
            data="100.0",
            observationTime="2025-01-01T00:00:00Z",
            observationHash=f"hash-{topic}",
            isPrediction=True
        )
        # Should handle all these topic formats
        assert result in [None, True], f"Failed for topic: {topic}"


@pytest.mark.slow
@pytest.mark.integration
def test_unicode_in_hash(client_instance, server_available):
    """Test that unicode characters in hash are handled."""
    result = client_instance.publish(
        topic="unicode-test",
        data="100.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="hash-with-émojis-🎉",
        isPrediction=True
    )

    # Should handle unicode
    assert result in [None, True]


@pytest.mark.slow
@pytest.mark.integration
def test_extremely_long_topic_name(client_instance, server_available):
    """Test behavior with extremely long topic names."""
    # 500 character topic name
    long_topic = "a" * 500

    result = client_instance.publish(
        topic=long_topic,
        data="100.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="long-topic-hash",
        isPrediction=True
    )

    # Should either handle or reject gracefully
    assert result in [None, True, False]


@pytest.mark.slow
@pytest.mark.integration
def test_empty_observation_hash(client_instance, server_available):
    """Test behavior when observation hash is empty string."""
    result = client_instance.publish(
        topic="empty-hash-test",
        data="100.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="",
        isPrediction=True
    )

    # Empty hash should be handled (likely rejected but gracefully)
    assert result in [None, True, False]


@pytest.mark.slow
@pytest.mark.integration
def test_malformed_timestamp_format(client_instance, server_available):
    """Test that malformed timestamps are handled."""
    # Try various invalid timestamp formats
    invalid_timestamps = [
        "2025-01-01",  # Missing time
        "not-a-timestamp",  # Completely invalid
        "2025/01/01 12:00:00",  # Wrong format
    ]

    for timestamp in invalid_timestamps:
        result = client_instance.publish(
            topic=f"timestamp-test-{timestamp[:10]}",
            data="100.0",
            observationTime=timestamp,
            observationHash="timestamp-hash",
            isPrediction=True
        )
        # Should handle gracefully (likely reject)
        assert result in [None, True, False], f"Failed for timestamp: {timestamp}"


@pytest.mark.slow
@pytest.mark.integration
def test_very_precise_decimal_value(client_instance, server_available):
    """Test prediction with many decimal places."""
    precise_value = "123.456789012345678901234567890"

    result = client_instance.publish(
        topic="precise-decimal-test",
        data=precise_value,
        observationTime="2025-01-01T00:00:00Z",
        observationHash="precise-hash",
        isPrediction=True
    )

    # Should handle high precision decimals
    assert result in [None, True]


@pytest.mark.slow
@pytest.mark.integration
def test_scientific_notation_value(client_instance, server_available):
    """Test that scientific notation is handled."""
    result = client_instance.publish(
        topic="scientific-notation-test",
        data="1.23e10",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="scientific-hash",
        isPrediction=True
    )

    # Scientific notation should be handled
    assert result in [None, True]


@pytest.mark.slow
@pytest.mark.integration
def test_concurrent_challenge_requests_uniqueness(server_url, server_available):
    """Test that rapid concurrent challenge requests all return unique tokens."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    num_requests = 20

    def get_challenge():
        response = requests.get(f"{server_url}/api/v1/auth/challenge")
        if response.status_code == 200:
            return response.json()["challenge"]
        return None

    # Make many concurrent requests
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(get_challenge) for _ in range(num_requests)]
        challenges = [future.result() for future in as_completed(futures)]

    # All should succeed
    assert all(challenges), "Some challenges failed to generate"

    # All should be unique (no collisions under load)
    unique_challenges = set(challenges)
    assert len(unique_challenges) == num_requests, \
        f"Only {len(unique_challenges)}/{num_requests} unique challenges (collision detected!)"


@pytest.mark.slow
@pytest.mark.integration
def test_observation_endpoint_with_no_data(server_url, server_available):
    """Test observation endpoint behavior when database is empty."""
    response = requests.get(f"{server_url}/api/v1/observation/get")

    assert response.status_code == 200

    data = response.json()
    # Should return None when no observations exist
    # This is valid - the endpoint should handle empty state gracefully
    assert data is None or isinstance(data, dict)


@pytest.mark.slow
@pytest.mark.integration
def test_prediction_with_missing_optional_fields(server_url, server_available, test_alias, challenge_token):
    """Test prediction submission with minimal required fields only."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    # Minimal payload - only required field
    minimal_payload = json.dumps({
        "value": "100.0"
    })

    response = requests.post(
        f"{server_url}/api/v1/prediction/post",
        headers=headers,
        json=minimal_payload
    )

    # Should either accept (200) or reject auth (401) or reject payload (422)
    # But not crash
    assert response.status_code in [200, 401, 422]


@pytest.mark.slow
@pytest.mark.integration
def test_health_endpoint_during_load(server_url, server_available):
    """Test that health endpoint remains responsive during load."""
    from concurrent.futures import ThreadPoolExecutor
    import time

    # Create background load
    def background_request():
        for _ in range(5):
            requests.get(f"{server_url}/api/v1/observation/get")
            time.sleep(0.05)

    # Start background load
    with ThreadPoolExecutor(max_workers=5) as executor:
        # Submit background tasks
        futures = [executor.submit(background_request) for _ in range(5)]

        # Check health endpoint remains responsive
        health_times = []
        for _ in range(10):
            start = time.time()
            response = requests.get(f"{server_url}/health")
            elapsed = (time.time() - start) * 1000

            assert response.status_code == 200
            health_times.append(elapsed)
            time.sleep(0.1)

        # Wait for background tasks
        for future in futures:
            future.result()

    # Health endpoint should remain fast even under load
    avg_health_time = sum(health_times) / len(health_times)
    assert avg_health_time < 150, f"Health endpoint degraded to {avg_health_time:.2f}ms under load"


@pytest.mark.slow
@pytest.mark.integration
def test_invalid_http_method_handling(server_url, server_available):
    """Test that endpoints reject invalid HTTP methods gracefully."""
    # Try POST on GET-only endpoint
    response = requests.post(f"{server_url}/health")

    # Should reject with 405 Method Not Allowed (or handle gracefully)
    assert response.status_code in [405, 200]  # Some frameworks auto-route

    # Server should remain stable
    health_check = requests.get(f"{server_url}/health")
    assert health_check.status_code == 200


@pytest.mark.slow
@pytest.mark.integration
def test_very_long_hash_string(client_instance, server_available):
    """Test behavior with extremely long hash strings."""
    # 10KB hash string
    long_hash = "a" * 10000

    result = client_instance.publish(
        topic="long-hash-test",
        data="100.0",
        observationTime="2025-01-01T00:00:00Z",
        observationHash=long_hash,
        isPrediction=True
    )

    # Should handle or reject gracefully (not crash)
    assert result in [None, True, False]


@pytest.mark.slow
@pytest.mark.integration
def test_rapid_sequential_requests_same_endpoint(server_url, server_available):
    """Test that rapid sequential requests to same endpoint don't cause issues."""
    errors = []

    for i in range(50):
        try:
            response = requests.get(f"{server_url}/api/v1/observation/get", timeout=2)
            if response.status_code != 200:
                errors.append(f"Request {i}: status {response.status_code}")
        except Exception as e:
            errors.append(f"Request {i}: {str(e)}")

    # Should handle all requests successfully
    error_rate = len(errors) / 50 * 100
    assert error_rate < 5, f"{error_rate:.1f}% error rate: {errors[:5]}"


@pytest.mark.slow
@pytest.mark.integration
def test_challenge_token_expiry_handling(server_url, server_available, test_alias):
    """Test that old/reused challenge tokens are handled appropriately."""
    # Get a challenge token
    response = requests.get(f"{server_url}/api/v1/auth/challenge")
    old_challenge = response.json()["challenge"]

    # Use it once
    headers1 = test_alias.authPayload(asDict=True, challenge=old_challenge)
    response1 = requests.get(f"{server_url}/api/v1/balance/get", headers=headers1)

    # Try to reuse the same challenge (might be rejected depending on implementation)
    headers2 = test_alias.authPayload(asDict=True, challenge=old_challenge)
    response2 = requests.get(f"{server_url}/api/v1/balance/get", headers=headers2)

    # Both should get auth response (401 with mock wallet)
    # Implementation may reject reused challenges or allow them
    assert response1.status_code in [200, 401]
    assert response2.status_code in [200, 401]


@pytest.mark.slow
@pytest.mark.integration
def test_whitespace_in_values(client_instance, server_available):
    """Test handling of whitespace in various fields."""
    # Leading/trailing whitespace in data
    result = client_instance.publish(
        topic="whitespace-test",
        data="  100.50  ",
        observationTime="2025-01-01T00:00:00Z",
        observationHash="whitespace-hash",
        isPrediction=True
    )

    # Should handle whitespace gracefully
    assert result in [None, True, False]
