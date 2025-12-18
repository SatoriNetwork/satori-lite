#!/bin/bash
set -e

echo "Starting Satori Lite..."

# Function to handle shutdown gracefully
shutdown() {
    echo "Shutting down Satori Lite..."
    kill -TERM "$NEURON_PID" "$WEB_PID" 2>/dev/null || true
    wait "$NEURON_PID" "$WEB_PID" 2>/dev/null || true
    exit 0
}

# Trap signals for graceful shutdown
trap shutdown SIGTERM SIGINT

# Start the neuron in the background
echo "Starting Satori Neuron..."
cd /Satori/Neuron
python start.py &
NEURON_PID=$!
echo "Neuron started with PID $NEURON_PID"

# Wait a moment for neuron to initialize
sleep 2

# Start the web UI in the background
echo "Starting Web UI on port ${WEB_PORT:-24601}..."
cd /Satori/Web
python app.py &
WEB_PID=$!
echo "Web UI started with PID $WEB_PID"

echo ""
echo "================================================"
echo "Satori Lite is running!"
echo "Web UI available at: http://localhost:${WEB_PORT:-24601}"
echo "================================================"
echo ""

# Wait for both processes
wait "$NEURON_PID" "$WEB_PID"
