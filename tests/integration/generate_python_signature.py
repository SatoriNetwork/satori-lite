#!/usr/bin/env python3
"""
Generate a Python signature for Dart compatibility testing.

This script uses satori-lite's wallet code to generate a signature
that can be compared with Dart's output.
"""
import sys
from pathlib import Path

# Add satorilib to path
SATORI_LITE_PATH = Path(__file__).parent.parent.parent.parent / "satorilib" / "src"
if str(SATORI_LITE_PATH) not in sys.path:
    sys.path.insert(0, str(SATORI_LITE_PATH))

try:
    from satorilib.wallet.evrmore.sign import signMessage
    from satorilib.wallet.evrmore.verify import verify
    from evrmore.wallet import CEvrmoreSecret

    # Known test WIF (same as Dart will use)
    test_wif = 'cW2YkiSzKVcG3VVKKBhVpAJFKdigjfXgDE2fxkk6YJW9Q9qoRUSc'
    test_message = "test message 123"

    print("\n" + "=" * 70)
    print("PYTHON SIGNATURE GENERATION (satori-lite)")
    print("=" * 70)
    print()

    # Create key from WIF
    key = CEvrmoreSecret(test_wif)

    # Get pubkey
    pubkey_hex = key.pub.hex()

    # Sign message
    signature_bytes = signMessage(key, test_message)
    signature_base64 = signature_bytes.decode('ascii')

    print(f"WIF:         {test_wif}")
    print(f"Pubkey:      {pubkey_hex}")
    print(f"Message:     \"{test_message}\"")
    print(f"Signature:   {signature_base64}")
    print()

    # Verify signature in Python (sanity check)
    is_valid = verify(
        message=test_message,
        signature=signature_bytes,
        publicKey=pubkey_hex,
    )

    if is_valid:
        print("✅ Python self-verification: PASSED")
    else:
        print("❌ Python self-verification: FAILED")
        sys.exit(1)

    print()
    print("=" * 70)
    print("EXPECTED DART OUTPUT")
    print("=" * 70)
    print(f"Pubkey:      {pubkey_hex}")
    print(f"Signature:   {signature_base64}")
    print()
    print("If Dart SignMessage produces these exact values:")
    print("✅ Cross-language compatibility is CONFIRMED!")
    print("=" * 70)
    print()

except ImportError as e:
    print(f"❌ ERROR: Required Python libraries not installed: {e}")
    print()
    print("To install dependencies:")
    print("  cd /app/central-lite/satori-lite")
    print("  pip install -r requirements.txt")
    print()
    sys.exit(1)
except Exception as e:
    print(f"❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
