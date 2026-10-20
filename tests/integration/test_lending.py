"""
Integration tests for lending operations.

Tests lender relationship creation, removal, and status checking.
"""
import pytest
import requests

# Pools are named by their Base address (a throwaway one).
POOL_ADDRESS = "0x52908400098527886E0F7030069857D2E4169EE7"


@pytest.mark.integration
@pytest.mark.lending
def test_create_lending_requires_auth(server_url, server_available):
    """Test POST /api/v1/lender/lend requires authentication."""
    response = requests.post(
        f"{server_url}/api/v1/lender/lend",
        json={"pool_address": POOL_ADDRESS}
    )

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.lending
def test_delete_lending_requires_auth(server_url, server_available):
    """Test DELETE /api/v1/lender/lend requires authentication."""
    response = requests.delete(f"{server_url}/api/v1/lender/lend")

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.lending
def test_get_lender_status_requires_auth(server_url, server_available):
    """Test GET /api/v1/lender/status requires authentication."""
    response = requests.get(f"{server_url}/api/v1/lender/status")

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.lending
def test_create_lending_with_invalid_auth(server_url, server_available, challenge_token, test_wallet):
    """Test POST lending with invalid authentication."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    response = requests.post(
        f"{server_url}/api/v1/lender/lend",
        headers=headers,
        json={"pool_address": POOL_ADDRESS}
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.lending
def test_delete_lending_with_invalid_auth(server_url, server_available, challenge_token, test_wallet):
    """Test DELETE lending with invalid authentication."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    response = requests.delete(
        f"{server_url}/api/v1/lender/lend",
        headers=headers
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.lending
def test_get_lender_status_with_invalid_auth(server_url, server_available, challenge_token, test_wallet):
    """Test GET lender status with invalid authentication."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    response = requests.get(
        f"{server_url}/api/v1/lender/status",
        headers=headers
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.lending
def test_lending_payload_structure(server_url, server_available, challenge_token, test_wallet):
    """Test lending payload is correctly structured."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    payload = {"pool_address": POOL_ADDRESS}

    response = requests.post(
        f"{server_url}/api/v1/lender/lend",
        headers=headers,
        json=payload
    )

    # Will fail auth (401) but tests that payload structure is accepted
    # If we got 400, it means payload structure is wrong
    assert response.status_code != 400, "Payload structure should be valid"
    assert response.status_code == 401  # Auth failure expected


@pytest.mark.integration
@pytest.mark.lending
def test_lending_endpoints_exist(server_url, server_available, challenge_token, test_wallet):
    """Test all lending endpoints exist and are configured."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    # Test POST /api/v1/lender/lend
    response1 = requests.post(
        f"{server_url}/api/v1/lender/lend",
        headers=headers,
        json={"pool_address": POOL_ADDRESS}
    )
    assert response1.status_code != 404

    # Test DELETE /api/v1/lender/lend
    response2 = requests.delete(
        f"{server_url}/api/v1/lender/lend",
        headers=headers
    )
    assert response2.status_code != 404

    # Test GET /api/v1/lender/status
    response3 = requests.get(
        f"{server_url}/api/v1/lender/status",
        headers=headers
    )
    assert response3.status_code != 404
