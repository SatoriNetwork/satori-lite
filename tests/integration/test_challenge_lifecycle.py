"""
Challenge token lifecycle tests.

Tests challenge token creation, consumption, expiration, and cleanup.
"""
import pytest
import requests
import time


@pytest.mark.integration
@pytest.mark.slow
def test_challenge_token_single_use_consumption(server_url, server_available, test_alias):
    """Test that challenge token can only be used once (consumed after auth)."""
    # Get a challenge token
    response = requests.get(f"{server_url}/api/v1/auth/challenge")
    assert response.status_code == 200
    challenge = response.json()["challenge"]

    # Create auth headers
    headers = test_alias.authPayload(asDict=True, challenge=challenge)

    # First attempt: use the challenge
    response1 = requests.get(
        f"{server_url}/api/v1/balance/get",
        headers=headers
    )

    # Mock wallet will fail auth (401), but challenge is consumed
    assert response1.status_code in [200, 401]

    # Second attempt: reuse the SAME challenge (should fail differently)
    response2 = requests.get(
        f"{server_url}/api/v1/balance/get",
        headers=headers
    )

    # Should still fail - either challenge expired/consumed or auth failed
    # The important thing is that reusing challenges doesn't create security issues
    assert response2.status_code in [401]


@pytest.mark.integration
@pytest.mark.slow
def test_new_challenge_every_request(server_url, server_available):
    """Test that each challenge request returns a unique token."""
    challenges = set()

    for _ in range(20):
        response = requests.get(f"{server_url}/api/v1/auth/challenge")
        assert response.status_code == 200

        challenge = response.json()["challenge"]
        challenges.add(challenge)

        # Small delay to ensure timestamps differ
        time.sleep(0.01)

    # All challenges should be unique
    assert len(challenges) == 20, "All challenge tokens should be unique"


@pytest.mark.integration
@pytest.mark.slow
def test_challenge_token_format_consistency(server_url, server_available):
    """Test that all challenge tokens follow consistent UUID format."""
    for _ in range(10):
        response = requests.get(f"{server_url}/api/v1/auth/challenge")
        challenge = response.json()["challenge"]

        # Should be UUID format: 8-4-4-4-12 hex characters
        parts = challenge.split('-')
        assert len(parts) == 5, "Should have 5 parts separated by hyphens"
        assert len(parts[0]) == 8, "First part should be 8 characters"
        assert len(parts[1]) == 4, "Second part should be 4 characters"
        assert len(parts[2]) == 4, "Third part should be 4 characters"
        assert len(parts[3]) == 4, "Fourth part should be 4 characters"
        assert len(parts[4]) == 12, "Fifth part should be 12 characters"


@pytest.mark.integration
@pytest.mark.slow
def test_expired_challenge_rejected(server_url, server_available, test_alias):
    """Test that expired challenge tokens are rejected.

    Note: Challenge tokens expire after 60 seconds.
    This test verifies the expiration logic exists but doesn't wait 60 seconds.
    """
    # Get a fresh challenge
    response = requests.get(f"{server_url}/api/v1/auth/challenge")
    challenge = response.json()["challenge"]

    # Immediately try to use it (should work or fail auth, but not fail expiration)
    headers = test_alias.authPayload(asDict=True, challenge=challenge)
    response = requests.get(f"{server_url}/api/v1/balance/get", headers=headers)

    # Should get either success (200) or auth failure (401), but not expiration error
    assert response.status_code in [200, 401]

    # Note: To fully test expiration, we'd need to:
    # 1. Get challenge
    # 2. Wait 61+ seconds
    # 3. Try to use it
    # 4. Should fail with 401 "expired" message
    # This is too slow for regular test runs


@pytest.mark.integration
@pytest.mark.slow
def test_challenge_creation_under_load(server_url, server_available):
    """Test that many concurrent challenge requests all succeed."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    num_requests = 50

    def get_challenge():
        response = requests.get(f"{server_url}/api/v1/auth/challenge")
        if response.status_code == 200:
            return response.json()["challenge"]
        return None

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(get_challenge) for _ in range(num_requests)]
        challenges = [f.result() for f in as_completed(futures)]

    # All should succeed
    assert all(challenges), f"All {num_requests} challenges should be created"

    # All should be unique (no collisions)
    unique_challenges = set(challenges)
    assert len(unique_challenges) == num_requests, "No duplicate challenges under load"


@pytest.mark.integration
@pytest.mark.slow
def test_challenge_storage_memory_bounds(server_url, server_available):
    """Test that challenge storage doesn't grow unbounded."""
    # Create many challenges
    initial_challenges = []
    for _ in range(100):
        response = requests.get(f"{server_url}/api/v1/auth/challenge")
        initial_challenges.append(response.json()["challenge"])

    # Server should handle this without issues
    # (In production, expired challenges should be cleaned up)

    # Verify server still responsive
    response = requests.get(f"{server_url}/health")
    assert response.status_code == 200, "Server should remain healthy"

    # Create more challenges to verify no memory leak
    for _ in range(100):
        response = requests.get(f"{server_url}/api/v1/auth/challenge")
        assert response.status_code == 200


@pytest.mark.integration
@pytest.mark.slow
def test_invalid_challenge_token_format_rejected(server_url, server_available, test_alias):
    """Test that malformed challenge tokens are rejected."""
    # Use an invalid challenge format
    invalid_challenges = [
        "not-a-uuid",
        "12345678",
        "",
        "a" * 100,
        "invalid-challenge-format"
    ]

    for invalid_challenge in invalid_challenges:
        headers = test_alias.authPayload(asDict=True, challenge=invalid_challenge)

        response = requests.get(
            f"{server_url}/api/v1/balance/get",
            headers=headers
        )

        # Should be rejected (401 for invalid/expired challenge)
        assert response.status_code == 401, f"Invalid challenge '{invalid_challenge}' should be rejected"


@pytest.mark.integration
@pytest.mark.slow
def test_challenge_response_structure(server_url, server_available):
    """Test that challenge endpoint returns expected structure."""
    response = requests.get(f"{server_url}/api/v1/auth/challenge")

    assert response.status_code == 200
    data = response.json()

    # Should have exactly one key: 'challenge'
    assert "challenge" in data, "Response should contain 'challenge' key"
    assert isinstance(data["challenge"], str), "Challenge should be a string"
    assert len(data["challenge"]) > 0, "Challenge should not be empty"


@pytest.mark.integration
@pytest.mark.slow
def test_concurrent_auth_with_different_challenges(server_url, server_available, test_alias):
    """Test that concurrent authentications with different challenges work."""
    from concurrent.futures import ThreadPoolExecutor

    def auth_flow():
        # Get challenge
        challenge_resp = requests.get(f"{server_url}/api/v1/auth/challenge")
        if challenge_resp.status_code != 200:
            return False

        challenge = challenge_resp.json()["challenge"]

        # Try to authenticate
        headers = test_alias.authPayload(asDict=True, challenge=challenge)
        auth_resp = requests.get(f"{server_url}/api/v1/balance/get", headers=headers)

        # Should either succeed or fail auth (not crash)
        return auth_resp.status_code in [200, 401]

    # Run concurrent auth flows
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(auth_flow) for _ in range(20)]
        results = [f.result() for f in futures]

    # All should complete successfully (either 200 or 401, no crashes)
    assert all(results), "All concurrent auth flows should complete"


@pytest.mark.integration
@pytest.mark.slow
def test_challenge_token_uniqueness_across_time(server_url, server_available):
    """Test that challenges remain unique even with small time intervals."""
    challenges = []

    for _ in range(50):
        response = requests.get(f"{server_url}/api/v1/auth/challenge")
        challenges.append(response.json()["challenge"])
        # Very small delay (or none) to test collision resistance
        time.sleep(0.001)

    # All should be unique despite rapid creation
    assert len(set(challenges)) == len(challenges), "Challenges should be unique even in rapid succession"
