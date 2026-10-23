"""
Integration test fixtures for neuron client + server tests.

These fixtures provide real server connections and authentication
for integration testing.
"""
import os
import pytest
import requests
import time
from typing import Optional


@pytest.fixture(scope="session")
def server_url():
    """
    Get the server URL for integration tests.

    Can be overridden via SATORI_SERVER_URL environment variable.
    Defaults to http://localhost:8000
    """
    return os.environ.get("SATORI_SERVER_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def server_available(server_url):
    """
    Check if the server is available before running integration tests.

    Skips tests if server is not reachable.
    """
    try:
        response = requests.get(f"{server_url}/health", timeout=2)
        if response.status_code == 200:
            return True
    except requests.exceptions.RequestException:
        pass

    pytest.skip(
        f"Server not available at {server_url}. "
        "Start the server with: docker-compose up"
    )


@pytest.fixture
def challenge_token(server_url, server_available):
    """
    Get a fresh challenge token from the server.

    Returns:
        str: Challenge token from /api/v1/auth/challenge
    """
    response = requests.get(f"{server_url}/api/v1/auth/challenge")
    assert response.status_code == 200, "Failed to get challenge token"

    data = response.json()
    assert "challenge" in data, "Challenge not in response"

    return data["challenge"]


@pytest.fixture
def authenticated_headers(test_alias, challenge_token):
    """
    Create authenticated request headers for testing.

    Args:
        test_alias: Test alias fixture
        challenge_token: Fresh challenge token

    Returns:
        dict: Headers with alias-pubkey, message, and signature
    """
    # Sign the challenge EIP-191 (personal_sign), the way satorilib signs for central
    return {
        'alias-pubkey': test_alias.pubkey,
        'message': challenge_token,
        'signature': test_alias.ethSign(challenge_token)
    }


@pytest.fixture
def database_url():
    """
    Get the database URL for integration tests.

    For integration tests, we typically use SQLite in-memory.
    The server should be configured to use this as well.
    """
    return os.environ.get("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture(autouse=True)
def rate_limit_delay():
    """
    Add a small delay between tests to avoid rate limiting.

    This is especially important for prediction submission tests
    which have a minimum cadence requirement.
    """
    yield
    time.sleep(0.1)  # 100ms delay between tests


@pytest.fixture
def cleanup_peer(server_url, authenticated_headers):
    """
    Cleanup fixture to remove test peers after integration tests.

    Note: Currently we don't have a delete peer endpoint,
    so this is a placeholder for future cleanup logic.
    """
    yield

    # TODO: Add cleanup logic when delete peer endpoint is available
    # For now, test database should be reset between test runs
