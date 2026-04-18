"""Tests for Nostr-to-Evrmore address derivation and key normalization."""

import importlib.util
import os
import sys

# Load the module without importing the full satorineuron package
_mod_path = os.path.join(
    os.path.dirname(__file__), '..', 'neuron-lite',
    'satorineuron', 'common', 'nostr_evrmore.py')
_spec = importlib.util.spec_from_file_location('nostr_evrmore', _mod_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

nostr_pubkey_to_evr_address = _mod.nostr_pubkey_to_evr_address
normalize_nostr_secret = _mod.normalize_nostr_secret
_scalar_mult = _mod._scalar_mult
_Gx = _mod._Gx
_Gy = _mod._Gy
_SECP256K1_N = _mod._SECP256K1_N


class TestAddressDerivation:

    def test_returns_base58_address(self):
        # Known 32-byte x-only pubkey (hex)
        pubkey = 'a' * 64
        addr = nostr_pubkey_to_evr_address(pubkey)
        assert isinstance(addr, str)
        assert addr[0] == 'E'  # Evrmore mainnet addresses start with E

    def test_deterministic(self):
        pubkey = '0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f817'
        addr1 = nostr_pubkey_to_evr_address(pubkey)
        addr2 = nostr_pubkey_to_evr_address(pubkey)
        assert addr1 == addr2

    def test_different_pubkeys_give_different_addresses(self):
        a1 = nostr_pubkey_to_evr_address('a' * 64)
        a2 = nostr_pubkey_to_evr_address('b' * 64)
        assert a1 != a2

    def test_invalid_length_raises(self):
        import pytest
        with pytest.raises(ValueError):
            nostr_pubkey_to_evr_address('abcd')


class TestKeyNormalization:

    def test_returns_expected_keys(self):
        # Use a known secret
        secret = format(12345, '064x')
        result = normalize_nostr_secret(secret)
        assert 'secret_hex' in result
        assert 'was_negated' in result
        assert 'nostr_pubkey' in result
        assert 'evr_compressed' in result
        assert 'evr_address' in result

    def test_normalized_key_always_even_y(self):
        """The normalized secret must always produce even y."""
        import random
        G = (_Gx, _Gy)
        for _ in range(50):
            d = random.randint(1, _SECP256K1_N - 1)
            secret_hex = format(d, '064x')
            result = normalize_nostr_secret(secret_hex)
            # Verify the resulting key produces even y
            d2 = int(result['secret_hex'], 16)
            P = _scalar_mult(d2, G)
            assert P[1] % 2 == 0, 'Normalized key should produce even y'

    def test_address_matches_pubkey_derivation(self):
        """The address from normalize_nostr_secret must match
        nostr_pubkey_to_evr_address for the same x-coordinate."""
        import random
        G = (_Gx, _Gy)
        for _ in range(50):
            d = random.randint(1, _SECP256K1_N - 1)
            secret_hex = format(d, '064x')
            result = normalize_nostr_secret(secret_hex)
            # Derive address from pubkey alone
            addr_from_pubkey = nostr_pubkey_to_evr_address(
                result['nostr_pubkey'])
            assert result['evr_address'] == addr_from_pubkey

    def test_even_y_key_not_negated(self):
        """A key that already has even y should not be negated."""
        G = (_Gx, _Gy)
        # Find a key with even y
        for d in range(1, 1000):
            P = _scalar_mult(d, G)
            if P[1] % 2 == 0:
                result = normalize_nostr_secret(format(d, '064x'))
                assert not result['was_negated']
                assert result['secret_hex'] == format(d, '064x')
                break

    def test_odd_y_key_is_negated(self):
        """A key with odd y should be negated."""
        G = (_Gx, _Gy)
        for d in range(1, 1000):
            P = _scalar_mult(d, G)
            if P[1] % 2 != 0:
                result = normalize_nostr_secret(format(d, '064x'))
                assert result['was_negated']
                assert result['secret_hex'] == format(
                    _SECP256K1_N - d, '064x')
                break

    def test_compressed_pubkey_starts_with_02(self):
        """The compressed pubkey should always start with 02."""
        import random
        for _ in range(20):
            d = random.randint(1, _SECP256K1_N - 1)
            result = normalize_nostr_secret(format(d, '064x'))
            assert result['evr_compressed'].startswith('02')
