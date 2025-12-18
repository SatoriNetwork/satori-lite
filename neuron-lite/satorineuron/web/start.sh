#!/bin/bash
set -e

# Map ENV to SATORI_ENV if not already set
# This provides compatibility with docker-compose.yaml files that use ENV=prod
export SATORI_ENV="${SATORI_ENV:-${ENV:-prod}}"

# Set UI port from environment variable
export SATORI_UI_PORT="${SATORI_UI_PORT:-24601}"

echo "Starting Satori Neuron..."
echo "Environment: $SATORI_ENV"
echo "UI Port: $SATORI_UI_PORT"

# Start the Satori Neuron
exec python /Satori/Neuron/start.py
