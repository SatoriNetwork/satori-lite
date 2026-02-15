#!/bin/sh
# Generate NIP-11 JSON from environment variables.
# Validates NOSTR_PUBKEY is a 64-char lowercase hex string.
# This script is embedded in docker-compose.yml but kept here as reference.

set -e

if [ -z "$NOSTR_PUBKEY" ]; then
  echo "ERROR: NOSTR_PUBKEY environment variable is required."
  echo "Set it to your neuron's 64-character lowercase hex Nostr public key."
  exit 1
fi

# Normalize to lowercase
NOSTR_PUBKEY=$(echo "$NOSTR_PUBKEY" | tr '[:upper:]' '[:lower:]')

# Validate format: exactly 64 hex characters
echo "$NOSTR_PUBKEY" | grep -qE '^[0-9a-f]{64}$' || {
  echo "ERROR: NOSTR_PUBKEY must be exactly 64 lowercase hex characters."
  echo "Got: $NOSTR_PUBKEY"
  exit 1
}

RELAY_NAME="${RELAY_NAME:-Satori Relay}"
RELAY_DESCRIPTION="${RELAY_DESCRIPTION:-A Satori Network relay}"
RELAY_CONTACT="${RELAY_CONTACT:-}"

cat > /out/nip11.json <<EOF
{
  "name": "${RELAY_NAME}",
  "description": "${RELAY_DESCRIPTION}",
  "pubkey": "${NOSTR_PUBKEY}",
  "self": "${NOSTR_PUBKEY}",
  "contact": "${RELAY_CONTACT}",
  "supported_nips": [1, 2, 4, 9, 11, 22, 28, 40, 70],
  "software": "https://github.com/hoytech/strfry",
  "version": "strfry"
}
EOF

echo "NIP-11 document generated:"
cat /out/nip11.json
