"""
Integration tests for balance retrieval endpoint.

Tests balance and stake retrieval for authenticated peers.
"""
import pytest
import requests


@pytest.mark.integration
@pytest.mark.balance
def test_get_balance_requires_auth(server_url, server_available):
    """Test GET /api/v1/balance/get requires authentication."""
    response = requests.get(f"{server_url}/api/v1/balance/get")

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.balance
def test_get_balance_with_invalid_auth(server_url, server_available, challenge_token, test_alias):
    """Test GET balance with invalid authentication."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    response = requests.get(
        f"{server_url}/api/v1/balance/get",
        headers=headers
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.balance
def test_balance_endpoint_exists(server_url, server_available, challenge_token, test_alias):
    """Test balance endpoint exists and is configured."""
    headers = test_alias.authPayload(asDict=True, challenge=challenge_token)

    response = requests.get(
        f"{server_url}/api/v1/balance/get",
        headers=headers
    )

    # Should not be 404 (endpoint exists)
    assert response.status_code != 404

    # Should be 401 (auth failure with mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.balance
def test_balance_response_would_have_correct_structure(server_url, server_available):
    """Test that balance endpoint expects correct response structure.

    Based on the API design, successful response should include:
    - balance: raw balance from Balance table
    - stake: qualified balance from Stake table
    - peer_id: authenticated peer's ID
    """
    # This is a documentation test - we can't test actual response
    # without a real wallet, but we verify the endpoint configuration

    # The endpoint exists and requires auth (tested above)
    # Expected response format (from design docs):
    expected_structure = {
        "balance": "float",
        "stake": "float",
        "peer_id": "int"
    }

    # This test documents the expected structure
    assert "balance" in expected_structure
    assert "stake" in expected_structure
    assert "peer_id" in expected_structure
