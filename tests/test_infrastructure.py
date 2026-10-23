"""
Test infrastructure smoke tests.

Verifies that the test setup is working correctly with REAL implementations.
"""
import pytest


def test_pytest_works():
    """Verify pytest is working."""
    assert True


def test_fixtures_available(test_alias, test_server_url, client_instance):
    """Verify that common fixtures are available."""
    assert test_alias is not None
    assert test_server_url is not None
    assert client_instance is not None


def test_alias_fixture_is_real(test_alias):
    """Verify the test alias is a REAL EvrmoreIdentity, not a mock."""
    from satorilib.wallet.evrmore.identity import EvrmoreIdentity
    assert isinstance(test_alias, EvrmoreIdentity), "test_alias should be a real EvrmoreIdentity"


def test_alias_fixture_structure(test_alias):
    """Verify the test alias has required attributes."""
    # The real key class exposes 'pubkey', NOT 'publicKey'
    assert hasattr(test_alias, 'pubkey')
    assert hasattr(test_alias, 'address')
    assert hasattr(test_alias, 'sign')
    assert hasattr(test_alias, 'ethSign')
    assert hasattr(test_alias, 'verify')
    assert hasattr(test_alias, 'authPayload')

    # Verify pubkey format (66 chars, hex, starts with 02 or 03)
    assert len(test_alias.pubkey) == 66, f"pubkey should be 66 chars, got {len(test_alias.pubkey)}"
    assert test_alias.pubkey.startswith(('02', '03')), f"pubkey should start with 02 or 03, got {test_alias.pubkey[:2]}"

    # Verify it's valid hex
    try:
        bytes.fromhex(test_alias.pubkey)
    except ValueError:
        pytest.fail(f"pubkey should be valid hex: {test_alias.pubkey}")


def test_alias_can_sign(test_alias):
    """Verify the alias can actually sign messages."""
    message = "test message to sign"
    signature = test_alias.sign(message)

    # Signature should be bytes
    assert isinstance(signature, bytes), f"signature should be bytes, got {type(signature)}"
    assert len(signature) > 0, "signature should not be empty"


def test_alias_can_verify_own_signature(test_alias):
    """Verify the alias can verify its own signatures."""
    message = "test message to verify"
    signature = test_alias.sign(message)

    # Should verify its own signature
    is_valid = test_alias.verify(message, signature)
    assert is_valid, "the alias should verify its own signature"


def test_alias_auth_payload(test_alias):
    """Verify authPayload generates correct structure."""
    challenge = "test-challenge-uuid"
    payload = test_alias.authPayload(asDict=True, challenge=challenge)

    assert isinstance(payload, dict), "authPayload should return dict when asDict=True"
    # The keys are the HTTP header names central's legacy auth reads (no address)
    assert set(payload) == {'alias-pubkey', 'message', 'signature'}
    assert payload['alias-pubkey'] == test_alias.pubkey
    assert payload['message'] == challenge

    # The signature is EIP-191 (personal_sign): 0x + r || s || v
    assert payload['signature'].startswith('0x') and len(payload['signature']) == 132
    assert payload['signature'] == test_alias.ethSign(challenge)


def test_client_instance_fixture(client_instance):
    """Verify client instance has required attributes."""
    assert hasattr(client_instance, "alias")
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
