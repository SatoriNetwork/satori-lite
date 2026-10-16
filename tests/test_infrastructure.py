"""
Test infrastructure smoke tests.

Verifies that the test setup is working correctly with REAL implementations.
"""
import pytest


def test_pytest_works():
    """Verify pytest is working."""
    assert True


def test_fixtures_available(test_wallet, test_server_url, client_instance):
    """Verify that common fixtures are available."""
    assert test_wallet is not None
    assert test_server_url is not None
    assert client_instance is not None


def test_wallet_fixture_is_real(test_wallet):
    """Verify test wallet is a REAL EvrmoreIdentity, not a mock."""
    from satorilib.wallet.evrmore.identity import EvrmoreIdentity
    assert isinstance(test_wallet, EvrmoreIdentity), "test_wallet should be a real EvrmoreIdentity"


def test_wallet_fixture_structure(test_wallet):
    """Verify test wallet has required attributes."""
    # Real wallet class exposes 'pubkey', NOT 'publicKey'
    assert hasattr(test_wallet, 'pubkey')
    assert hasattr(test_wallet, 'address')
    assert hasattr(test_wallet, 'sign')
    assert hasattr(test_wallet, 'ethSign')
    assert hasattr(test_wallet, 'verify')
    assert hasattr(test_wallet, 'authPayload')

    # Verify pubkey format (66 chars, hex, starts with 02 or 03)
    assert len(test_wallet.pubkey) == 66, f"pubkey should be 66 chars, got {len(test_wallet.pubkey)}"
    assert test_wallet.pubkey.startswith(('02', '03')), f"pubkey should start with 02 or 03, got {test_wallet.pubkey[:2]}"

    # Verify it's valid hex
    try:
        bytes.fromhex(test_wallet.pubkey)
    except ValueError:
        pytest.fail(f"pubkey should be valid hex: {test_wallet.pubkey}")


def test_wallet_can_sign(test_wallet):
    """Verify the wallet can actually sign messages."""
    message = "test message to sign"
    signature = test_wallet.sign(message)

    # Signature should be bytes
    assert isinstance(signature, bytes), f"signature should be bytes, got {type(signature)}"
    assert len(signature) > 0, "signature should not be empty"


def test_wallet_can_verify_own_signature(test_wallet):
    """Verify the wallet can verify its own signatures."""
    message = "test message to verify"
    signature = test_wallet.sign(message)

    # Should verify its own signature
    is_valid = test_wallet.verify(message, signature)
    assert is_valid, "wallet should verify its own signature"


def test_wallet_auth_payload(test_wallet):
    """Verify authPayload generates correct structure."""
    challenge = "test-challenge-uuid"
    payload = test_wallet.authPayload(asDict=True, challenge=challenge)

    assert isinstance(payload, dict), "authPayload should return dict when asDict=True"
    # The keys are the HTTP header names central's legacy auth reads
    assert set(payload) == {'wallet-pubkey', 'message', 'address', 'signature'}
    assert payload['wallet-pubkey'] == test_wallet.pubkey
    assert payload['message'] == challenge
    assert payload['address'] == test_wallet.address

    # The signature is EIP-191 (personal_sign): 0x + r || s || v
    assert payload['signature'].startswith('0x') and len(payload['signature']) == 132
    assert payload['signature'] == test_wallet.ethSign(challenge)


def test_client_instance_fixture(client_instance):
    """Verify client instance has required attributes."""
    assert hasattr(client_instance, 'wallet')
    assert hasattr(client_instance, 'url')
    assert hasattr(client_instance, 'publish')
    assert hasattr(client_instance, '_getChallenge')
    assert hasattr(client_instance, '_makeAuthenticatedCall')


@pytest.mark.unit
def test_unit_marker():
    """Verify unit test marker works."""
    assert True


@pytest.mark.integration
def test_integration_marker():
    """Verify integration test marker works."""
    assert True
