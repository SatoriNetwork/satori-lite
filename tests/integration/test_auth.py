"""
Integration tests for authentication flow.

Tests the challenge-response authentication mechanism with REAL wallets and server.
"""
import pytest
import requests
import base64


@pytest.mark.integration
@pytest.mark.auth
def test_get_challenge_token(test_server_url, server_available):
    """Test getting a challenge token from the server."""
    response = requests.get(f"{test_server_url}/api/v1/auth/challenge")

    assert response.status_code == 200
    data = response.json()
    assert "challenge" in data
    assert isinstance(data["challenge"], str)
    assert len(data["challenge"]) > 0


@pytest.mark.integration
@pytest.mark.auth
def test_challenge_token_is_unique(test_server_url, server_available):
    """Test that each challenge request returns a unique token."""
    response1 = requests.get(f"{test_server_url}/api/v1/auth/challenge")
    response2 = requests.get(f"{test_server_url}/api/v1/auth/challenge")

    assert response1.status_code == 200
    assert response2.status_code == 200

    token1 = response1.json()["challenge"]
    token2 = response2.json()["challenge"]

    # Tokens should be different
    assert token1 != token2


@pytest.mark.integration
@pytest.mark.auth
def test_challenge_token_format(test_server_url, server_available):
    """Test challenge token is in UUID format."""
    response = requests.get(f"{test_server_url}/api/v1/auth/challenge")

    assert response.status_code == 200
    challenge = response.json()["challenge"]

    # UUID v4 format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
    parts = challenge.split('-')
    assert len(parts) == 5, "UUID should have 5 parts separated by hyphens"
    assert len(parts[0]) == 8
    assert len(parts[1]) == 4
    assert len(parts[2]) == 4
    assert len(parts[3]) == 4
    assert len(parts[4]) == 12


@pytest.mark.integration
@pytest.mark.auth
def test_client_can_get_challenge(client_instance, server_available):
    """Test SatoriServerClient can get challenge token."""
    challenge = client_instance._getChallenge()

    assert challenge is not None
    assert isinstance(challenge, str)
    assert len(challenge) > 0


@pytest.mark.integration
@pytest.mark.auth
def test_authenticated_call_with_real_wallet(test_server_url, server_available, authenticated_headers):
    """Test authenticated call with REAL wallet using valid signature."""
    # Make an authenticated request to a protected endpoint
    response = requests.get(
        f"{test_server_url}/api/v1/balance/get",
        headers=authenticated_headers
    )

    # With a real wallet and valid signature, should either:
    # - 200: Peer exists and has balance
    # - 404: Peer not found (new wallet, not registered)
    # Should NOT get 401 (invalid signature) with a real wallet
    assert response.status_code in [200, 404], f"Expected 200 or 404, got {response.status_code}: {response.text}"


@pytest.mark.integration
@pytest.mark.auth
def test_real_wallet_signature_is_valid(test_server_url, server_available, test_wallet):
    """Test that a real wallet produces a valid signature that the server accepts."""
    # Get a fresh challenge
    challenge_response = requests.get(f"{test_server_url}/api/v1/auth/challenge")
    assert challenge_response.status_code == 200
    challenge = challenge_response.json()["challenge"]

    # Sign with real wallet, EIP-191 (personal_sign)
    headers = {
        "wallet-pubkey": test_wallet.pubkey,
        "message": challenge,
        "signature": test_wallet.ethSign(challenge)
    }

    # Try to access an authenticated endpoint
    response = requests.get(
        f"{test_server_url}/api/v1/balance/get",
        headers=headers
    )

    # Should NOT get 401 - signature should be valid
    assert response.status_code != 401, f"Signature validation failed: {response.text}"


@pytest.mark.integration
@pytest.mark.auth
def test_missing_auth_headers_rejected(test_server_url, server_available):
    """Test that requests without auth headers are rejected."""
    # Try to access authenticated endpoint without headers
    response = requests.get(f"{test_server_url}/api/v1/balance/get")

    # Should get 422 or 401 (missing required headers)
    assert response.status_code in [401, 422]


@pytest.mark.integration
@pytest.mark.auth
def test_invalid_pubkey_format_rejected(test_server_url, server_available, challenge_token):
    """Test that invalid public key format is rejected."""
    headers = {
        "wallet-pubkey": "invalid_pubkey",  # Invalid format
        "message": challenge_token,
        "signature": "fake_signature"
    }

    response = requests.post(
        f"{test_server_url}/api/v1/prediction/post",
        headers=headers,
        json={"value": "100.0"}
    )

    # Should reject invalid pubkey format
    assert response.status_code in [400, 401]


@pytest.mark.integration
@pytest.mark.auth
def test_invalid_signature_rejected(test_server_url, server_available, test_wallet, challenge_token):
    """Test that invalid signatures are rejected."""
    headers = {
        "wallet-pubkey": test_wallet.pubkey,  # Valid pubkey
        "message": challenge_token,
        "signature": "invalid_signature_not_base64"  # Invalid signature
    }

    response = requests.get(
        f"{test_server_url}/api/v1/balance/get",
        headers=headers
    )

    # Should reject invalid signature
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.auth
def test_wrong_message_signature_rejected(test_server_url, server_available, test_wallet, challenge_token):
    """Test that signature for wrong message is rejected."""
    # Sign a DIFFERENT message than the challenge
    wrong_message = "wrong_message"

    headers = {
        "wallet-pubkey": test_wallet.pubkey,
        "message": challenge_token,  # This is the challenge
        "signature": test_wallet.ethSign(wrong_message)  # But signature is for different message
    }

    response = requests.get(
        f"{test_server_url}/api/v1/balance/get",
        headers=headers
    )

    # Should reject - signature doesn't match message
    assert response.status_code == 401
