"""
Shared pytest fixtures for neuron tests.

Uses REAL key implementations - NO mocking.
"""
import os
import sys
import pytest
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
def mock_response():
    """
    Create a mock HTTP response for unit tests that specifically need mocking.
    """
    from unittest.mock import MagicMock, Mock, PropertyMock

    response = MagicMock()
    response.status_code = 200
    response.text = '{"message": "success"}'
    response.json.return_value = {"message": "success"}
    # ok follows status_code, as requests.Response.ok does
    type(response).ok = PropertyMock(side_effect=lambda: response.status_code < 400)

    def raise_for_status():
        if response.status_code >= 400:
            from requests.exceptions import HTTPError
            raise HTTPError(f"HTTP {response.status_code}")

    response.raise_for_status = Mock(side_effect=raise_for_status)

    return response
