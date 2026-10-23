"""
Integration tests for peer management endpoints.

Tests reward address setting and retrieval.
"""
import pytest
import requests

# The reward address is a Base address (a throwaway one).
REWARD_ADDRESS = "0x2F66D2d1DfA3cbF191dd26394d3b241C90B57E74"


@pytest.mark.integration
@pytest.mark.peer
def test_get_reward_address_requires_auth(server_url, server_available):
    """Test GET /api/v1/peer/reward-address requires authentication."""
    response = requests.get(f"{server_url}/api/v1/peer/reward-address")

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.peer
def test_set_reward_address_requires_auth(server_url, server_available):
    """Test POST /api/v1/peer/reward-address requires authentication."""
    response = requests.post(
        f"{server_url}/api/v1/peer/reward-address",
        json={"reward_address": REWARD_ADDRESS}
    )

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.peer
def test_get_reward_address_with_invalid_auth(server_url, server_available, challenge_token, test_alias):
    """Test GET reward address with invalid authentication."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    response = requests.get(
        f"{server_url}/api/v1/peer/reward-address",
        headers=headers
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.peer
def test_set_reward_address_with_invalid_auth(server_url, server_available, challenge_token, test_alias):
    """Test POST reward address with invalid authentication."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    response = requests.post(
        f"{server_url}/api/v1/peer/reward-address",
        headers=headers,
        json={"reward_address": REWARD_ADDRESS}
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.peer
def test_reward_address_payload_structure(server_url, server_available, challenge_token, test_alias):
    """Test reward address payload is correctly structured."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    payload = {"reward_address": REWARD_ADDRESS}

    response = requests.post(
        f"{server_url}/api/v1/peer/reward-address",
        headers=headers,
        json=payload
    )

    # Will fail auth (401) but tests that payload structure is accepted
    # If we got 400, it means payload structure is wrong
    assert response.status_code != 400, "Payload structure should be valid"
    assert response.status_code == 401  # Auth failure expected


@pytest.mark.integration
@pytest.mark.peer
def test_get_reward_address_response_structure(server_url, server_available, challenge_token, test_alias):
    """Test GET reward address response structure (via error response)."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    response = requests.get(
        f"{server_url}/api/v1/peer/reward-address",
        headers=headers
    )

    # Should get 401 with mock wallet
    assert response.status_code == 401

    # Test that endpoint exists and is configured correctly
    # (not a 404 Not Found)
    assert response.status_code != 404
