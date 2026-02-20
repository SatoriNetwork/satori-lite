"""
Test cross-language signature compatibility between Dart and Python.

This test generates a signature in Python that can be verified against Dart output.
"""
import pytest
import sys
from pathlib import Path

# Add satorilib to path
SATORI_LITE_PATH = Path(__file__).parent.parent.parent / "satorilib" / "src"
if str(SATORI_LITE_PATH) not in sys.path:
    sys.path.insert(0, str(SATORI_LITE_PATH))


@pytest.mark.unit
def test_generate_python_signature_for_dart_verification():
    """
    Generate a Python signature using known test WIF.

    This signature can be verified in Dart to confirm cross-language compatibility.
    """
    from satorilib.wallet.evrmore.identity import EvrmoreIdentity
    from satorilib.wallet.evrmore.sign import signMessage
    from satorilib.wallet.evrmore.verify import verify
    from evrmore.wallet import CEvrmoreSecret
    import tempfile
    import os

    # Known test WIF (same as Dart will use)
    test_wif = 'cW2YkiSzKVcG3VVKKBhVpAJFKdigjfXgDE2fxkk6YJW9Q9qoRUSc'
    test_message = "test message 123"

    # Create key from WIF
    key = CEvrmoreSecret(test_wif)

    # Get pubkey
    pubkey_hex = key.pub.hex()

    # Sign message
    signature_bytes = signMessage(key, test_message)
    signature_base64 = signature_bytes.decode('ascii')

    print("\n" + "=" * 60)
    print("PYTHON SIGNATURE GENERATION")
    print("=" * 60)
    print(f"WIF: {test_wif}")
    print(f"Pubkey (hex): {pubkey_hex}")
    print(f"Message: \"{test_message}\"")
    print(f"Signature (base64): {signature_base64}")
    print("=" * 60)
    print("\nEXPECTED DART OUTPUT:")
    print("=" * 60)
    print(f"Pubkey: {pubkey_hex}")
    print(f"Signature: {signature_base64}")
    print("=" * 60)
    print("\nIf Dart produces these exact values:")
    print("✅ Cross-language compatibility CONFIRMED!")
    print("\n")

    # Verify signature in Python (sanity check)
    is_valid = verify(
        message=test_message,
        signature=signature_bytes,
        publicKey=pubkey_hex,
    )

    assert is_valid, "Python self-verification failed!"
    print("✅ Python self-verification passed\n")

    # Store values for assertions
    assert len(pubkey_hex) == 66, "Pubkey should be 66 hex characters"
    assert pubkey_hex.startswith(('02', '03')), "Compressed pubkey should start with 02 or 03"
    assert len(signature_base64) > 80, "Base64 signature should be ~88 characters"

    return {
        'pubkey': pubkey_hex,
        'signature': signature_base64,
        'message': test_message,
    }


if __name__ == '__main__':
    # Run directly without pytest
    result = test_generate_python_signature_for_dart_verification()
    print("\nTest completed successfully!")
    print(f"\nSave these values for Dart verification:")
    print(f"  Pubkey: {result['pubkey']}")
    print(f"  Signature: {result['signature']}")
