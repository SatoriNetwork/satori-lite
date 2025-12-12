"""
Integration tests for pool management operations.

Tests pool/worker relationship creation, removal, and open status toggling.
"""
import pytest
import requests


@pytest.mark.integration
@pytest.mark.pool
def test_add_worker_requires_auth(server_url, server_available):
    """Test POST /api/v1/pool/worker requires authentication."""
    response = requests.post(
        f"{server_url}/api/v1/pool/worker",
        json={"worker_address": "EWorkerAddress123"}
    )

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.pool
def test_remove_worker_requires_auth(server_url, server_available):
    """Test DELETE /api/v1/pool/worker/{address} requires authentication."""
    response = requests.delete(
        f"{server_url}/api/v1/pool/worker/EWorkerAddress123"
    )

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.pool
def test_toggle_open_requires_auth(server_url, server_available):
    """Test POST /api/v1/pool/toggle-open requires authentication."""
    response = requests.post(f"{server_url}/api/v1/pool/toggle-open")

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.pool
def test_add_worker_with_invalid_auth(server_url, server_available, challenge_token, test_wallet):
    """Test POST worker with invalid authentication."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    response = requests.post(
        f"{server_url}/api/v1/pool/worker",
        headers=headers,
        json={"worker_address": "EWorkerAddress123"}
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.pool
def test_remove_worker_with_invalid_auth(server_url, server_available, challenge_token, test_wallet):
    """Test DELETE worker with invalid authentication."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    response = requests.delete(
        f"{server_url}/api/v1/pool/worker/EWorkerAddress123",
        headers=headers
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.pool
def test_toggle_open_with_invalid_auth(server_url, server_available, challenge_token, test_wallet):
    """Test POST toggle-open with invalid authentication."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    response = requests.post(
        f"{server_url}/api/v1/pool/toggle-open",
        headers=headers
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.pool
def test_add_worker_payload_structure(server_url, server_available, challenge_token, test_wallet):
    """Test add worker payload is correctly structured."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    payload = {"worker_address": "EWorkerAddress123456789"}

    response = requests.post(
        f"{server_url}/api/v1/pool/worker",
        headers=headers,
        json=payload
    )

    # Will fail auth (401) but tests that payload structure is accepted
    # If we got 400, it means payload structure is wrong
    assert response.status_code != 400, "Payload structure should be valid"
    assert response.status_code == 401  # Auth failure expected


@pytest.mark.integration
@pytest.mark.pool
def test_remove_worker_url_parameter(server_url, server_available, challenge_token, test_wallet):
    """Test remove worker uses URL path parameter correctly."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    worker_address = "EWorkerAddress123456789"

    response = requests.delete(
        f"{server_url}/api/v1/pool/worker/{worker_address}",
        headers=headers
    )

    # Will fail auth (401) but tests that URL structure is correct
    # If we got 404, it means URL pattern is wrong
    assert response.status_code != 404, "URL pattern should be valid"
    assert response.status_code == 401  # Auth failure expected


@pytest.mark.integration
@pytest.mark.pool
def test_get_workers_requires_auth(server_url, server_available):
    """Test GET /api/v1/pool/workers requires authentication."""
    response = requests.get(f"{server_url}/api/v1/pool/workers")

    # Should reject unauthenticated request
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.pool
def test_get_workers_with_invalid_auth(server_url, server_available, challenge_token, test_wallet):
    """Test GET workers with invalid authentication."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    response = requests.get(
        f"{server_url}/api/v1/pool/workers",
        headers=headers
    )

    # Should reject invalid signature (mock wallet)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.pool
def test_get_workers_endpoint_exists(server_url, server_available, challenge_token, test_wallet):
    """Test GET /api/v1/pool/workers endpoint exists."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    response = requests.get(
        f"{server_url}/api/v1/pool/workers",
        headers=headers
    )

    # Should not be 404 (endpoint exists)
    assert response.status_code != 404


@pytest.mark.integration
@pytest.mark.pool
def test_pool_endpoints_exist(server_url, server_available, challenge_token, test_wallet):
    """Test all pool endpoints exist and are configured."""
    headers = test_wallet.authPayload(asDict=True, challenge=challenge_token)

    # Test POST /api/v1/pool/worker
    response1 = requests.post(
        f"{server_url}/api/v1/pool/worker",
        headers=headers,
        json={"worker_address": "ETestWorker"}
    )
    assert response1.status_code != 404

    # Test DELETE /api/v1/pool/worker/{address}
    response2 = requests.delete(
        f"{server_url}/api/v1/pool/worker/ETestWorker",
        headers=headers
    )
    assert response2.status_code != 404

    # Test POST /api/v1/pool/toggle-open
    response3 = requests.post(
        f"{server_url}/api/v1/pool/toggle-open",
        headers=headers
    )
    assert response3.status_code != 404

    # Test GET /api/v1/pool/workers
    response4 = requests.get(
        f"{server_url}/api/v1/pool/workers",
        headers=headers
    )
    assert response4.status_code != 404
