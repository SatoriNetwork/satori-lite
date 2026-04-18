"""Nostr-to-Evrmore address derivation and key normalization.

Both Nostr and Evrmore use secp256k1.  A Nostr public key is a 32-byte
x-only coordinate (BIP-340); an Evrmore compressed public key is 33 bytes
(02/03 prefix + x).  By convention we always derive the even-y (0x02)
address, and relay operators normalize their private key to match.
"""
import hashlib

import base58

# Evrmore P2PKH version byte (mainnet)
_EVR_P2PKH_VERSION = (33).to_bytes(1, 'big')  # 0x21

# secp256k1 curve order
_SECP256K1_N = (
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
)

# secp256k1 field prime
_SECP256K1_P = (
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
)

# Generator point
_Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _hash160(data: bytes) -> bytes:
    return hashlib.new(
        'ripemd160', hashlib.sha256(data).digest()).digest()


def _pubkey_to_evr_address(compressed_pubkey: bytes) -> str:
    h = _hash160(compressed_pubkey)
    payload = _EVR_P2PKH_VERSION + h
    checksum = hashlib.sha256(
        hashlib.sha256(payload).digest()).digest()[:4]
    return base58.b58encode(payload + checksum).decode()


def _point_add(P, Q):
    """Add two points on secp256k1."""
    p = _SECP256K1_P
    if P is None:
        return Q
    if Q is None:
        return P
    if P[0] == Q[0] and P[1] != Q[1]:
        return None
    if P == Q:
        lam = (3 * P[0] * P[0]) * pow(2 * P[1], p - 2, p) % p
    else:
        lam = (Q[1] - P[1]) * pow(Q[0] - P[0], p - 2, p) % p
    x = (lam * lam - P[0] - Q[0]) % p
    y = (lam * (P[0] - x) - P[1]) % p
    return (x, y)


def _scalar_mult(k, P):
    """Multiply a point by a scalar on secp256k1."""
    R = None
    Q = P
    while k:
        if k & 1:
            R = _point_add(R, Q)
        Q = _point_add(Q, Q)
        k >>= 1
    return R


def nostr_pubkey_to_evr_address(nostr_pubkey_hex: str) -> str:
    """Derive an Evrmore P2PKH address from a 32-byte Nostr public key.

    Always uses the even-y (0x02) convention.  The resulting address is
    spendable by the holder of the Nostr private key (after even-y
    normalization if needed).

    Args:
        nostr_pubkey_hex: 64-character hex string (32-byte x-only pubkey).

    Returns:
        Base58Check-encoded Evrmore address (e.g. "E...").
    """
    x_bytes = bytes.fromhex(nostr_pubkey_hex)
    if len(x_bytes) != 32:
        raise ValueError(
            f'Invalid Nostr pubkey length: expected 32 bytes, '
            f'got {len(x_bytes)}')
    compressed = b'\x02' + x_bytes
    return _pubkey_to_evr_address(compressed)


def normalize_nostr_secret(secret_hex: str) -> dict:
    """Normalize a Nostr private key to produce an even-y public key.

    If ``d * G`` has odd y, the function returns ``n - d`` which produces
    the same x-coordinate but with even y.  This ensures the Evrmore
    address derived from the Nostr pubkey (always 0x02) matches the
    address the wallet would derive from the private key.

    Args:
        secret_hex: 64-character hex Nostr private key.

    Returns:
        Dict with keys:
            secret_hex:      The (possibly negated) private key hex.
            was_negated:     True if the key was negated.
            nostr_pubkey:    The x-only public key hex (same either way).
            evr_compressed:  The 33-byte compressed pubkey hex (always 02+x).
            evr_address:     The Evrmore P2PKH address.
    """
    d = int(secret_hex, 16)
    G = (_Gx, _Gy)
    P = _scalar_mult(d, G)
    x_hex = format(P[0], '064x')

    negated = False
    if P[1] % 2 != 0:
        d = _SECP256K1_N - d
        negated = True

    compressed = '02' + x_hex
    address = _pubkey_to_evr_address(bytes.fromhex(compressed))

    return {
        'secret_hex': format(d, '064x'),
        'was_negated': negated,
        'nostr_pubkey': x_hex,
        'evr_compressed': compressed,
        'evr_address': address,
    }
