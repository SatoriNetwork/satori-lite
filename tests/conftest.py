"""
Shared pytest fixtures for neuron tests.

Uses REAL key implementations - NO mocking.
"""
import os
import sys
import tempfile
import pytest
import requests
from pathlib import Path

# Add satorilib to Python path
SATORI_LITE_PATH = Path(__file__).parent.parent.parent / "satorilib" / "src"
if str(SATORI_LITE_PATH) not in sys.path:
    sys.path.insert(0, str(SATORI_LITE_PATH))


@pytest.fixture
def test_server_url():
    """
    URL for the test server.

    Returns the server URL from environment or defaults to the Docker network URL.
    """
    return os.environ.get("SATORI_SERVER_URL", "http://satori-api:8000")


@pytest.fixture
def server_available(test_server_url):
    """Check if the test server is available."""
    try:
        response = requests.get(f"{test_server_url}/health", timeout=5)
        if response.status_code == 200:
            return True
    except requests.RequestException:
        pass
    pytest.skip(f"Server not available at {test_server_url}")


@pytest.fixture
def test_alias(tmp_path):
    """
    A REAL alias (the neuron's automated signing key) for authentication.

    An unencrypted EvrmoreIdentity in a temporary directory that can sign
    messages.
    """
    from satorilib.wallet.evrmore.identity import EvrmoreIdentity

    # auto-generates entropy since the file doesn't exist
    return EvrmoreIdentity(walletPath=str(tmp_path / "alias.yaml"))


@pytest.fixture
def challenge_token(test_server_url, server_available):
    """Get a real challenge token from the server."""
    response = requests.get(f"{test_server_url}/api/v1/auth/challenge", timeout=10)
    if response.status_code == 200:
        return response.json().get("challenge")
    pytest.skip("Could not get challenge token from server")


@pytest.fixture
def client_instance(test_alias, test_server_url):
    """
    Create a REAL SatoriServerClient instance for testing.

    Args:
        test_alias: Real EvrmoreIdentity alias fixture
        test_server_url: Server URL fixture

    Returns:
        SatoriServerClient configured with a real alias and server URL
    """
    from satorilib.server.server import SatoriServerClient

    client = SatoriServerClient(
        alias=test_alias,
        url=test_server_url
    )

    return client


@pytest.fixture
def authenticated_headers(test_alias, challenge_token):
    """
    Create real authenticated headers for API requests.

    Uses the real alias to sign a challenge and create valid auth headers.
    """
    # Sign the challenge EIP-191 (personal_sign), the way satorilib signs for central
    return {
        'alias-pubkey': test_alias.pubkey,
        'message': challenge_token,
        'signature': test_alias.ethSign(challenge_token)
    }


@pytest.fixture
def mock_response():
    """
    Create a mock HTTP response for unit tests that specifically need mocking.

    Note: Prefer using real server responses in integration tests.
    """
    from unittest.mock import MagicMock, Mock

    response = MagicMock()
    response.status_code = 200
    response.text = '{"message": "success"}'
    response.json.return_value = {"message": "success"}

    def raise_for_status():
        if response.status_code >= 400:
            from requests.exceptions import HTTPError
            raise HTTPError(f"HTTP {response.status_code}")

    response.raise_for_status = Mock(side_effect=raise_for_status)

    return response


# Markers for different test categories
def pytest_configure(config):
    """Configure custom pytest markers."""
    config.addinivalue_line(
        "markers", "unit: Unit tests (may use minimal mocking for isolation)"
    )
    config.addinivalue_line(
        "markers", "integration: Integration tests requiring running server"
    )
    config.addinivalue_line(
        "markers", "slow: Tests that take significant time to run"
    )
    config.addinivalue_line(
        "markers", "auth: Authentication-related tests"
    )
