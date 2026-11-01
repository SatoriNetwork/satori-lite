#!/bin/bash
#
# Install (or update) a Satori neuron.
#
# Installs the `satori` command and starts the neuron through it, with its
# wallet, config, models and data kept on disk in ~/satori/1 so they survive
# every update.
#
# Older versions of this script ran a container named `satori` that kept the
# wallet inside the container. If one exists, its keys (and config, models,
# data) are copied out to ~/satori/1 first, and the old container is stopped and
# renamed, never deleted, so nothing can be lost.
#
# Overridable: SATORI_HOME (default ~/satori), SATORI_INSTALL_DIR (default
# /usr/local/bin), SATORI_LEGACY_CONTAINER (default satori),
# SATORI_WRAPPER_URL (default: the satori script on main).

SATORI_HOME="${SATORI_HOME:-$HOME/satori}"
INSTALL_DIR="${SATORI_INSTALL_DIR:-/usr/local/bin}"
LEGACY="${SATORI_LEGACY_CONTAINER:-satori}"
WRAPPER_URL="${SATORI_WRAPPER_URL:-https://raw.githubusercontent.com/SatoriNetwork/satori-lite/main/satori}"
DATA="$SATORI_HOME/1"

fail() {
    echo ""
    echo "Error: $1"
    exit 1
}

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "Docker is not installed. Please install Docker first:"
    echo "  https://docs.docker.com/get-docker/"
    exit 1
fi
if ! docker info &> /dev/null; then
    fail "Docker is not running. Please start Docker."
fi

# Install the satori command
echo "Installing the satori command..."
if [ -w "$INSTALL_DIR" ]; then
    curl -fsSL "$WRAPPER_URL" -o "$INSTALL_DIR/satori" || fail "could not download the satori command."
    chmod +x "$INSTALL_DIR/satori"
else
    sudo curl -fsSL "$WRAPPER_URL" -o "$INSTALL_DIR/satori" || fail "could not download the satori command."
    sudo chmod +x "$INSTALL_DIR/satori"
fi

mkdir -p "$DATA/wallet" "$DATA/config" "$DATA/models" "$DATA/data"

has_keys() {
    ls "$1"/*.yaml &> /dev/null
}

# Rescue a neuron from an older install: its wallet lives inside the container
if docker ps -a --format '{{.Names}}' | grep -qx "$LEGACY"; then
    echo "Found a neuron from an older install (container '$LEGACY')."
    if has_keys "$DATA/wallet"; then
        echo "  $DATA/wallet already has keys; not copying over them."
    else
        echo "  Copying its wallet out to $DATA/wallet..."
        if ! docker cp "$LEGACY:/Satori/Neuron/wallet/." "$DATA/wallet/" 2> /tmp/satori-install-cp.err; then
            if grep -q "Could not find" /tmp/satori-install-cp.err; then
                echo "  It has no wallet yet; nothing to copy."
            else
                cat /tmp/satori-install-cp.err
                fail "could not copy the wallet out of '$LEGACY'. Nothing was changed; the old container is untouched."
            fi
        fi
        # The rest is worth keeping too, but the wallet is what matters
        docker cp "$LEGACY:/Satori/Neuron/config/." "$DATA/config/" &> /dev/null
        docker cp "$LEGACY:/Satori/models/." "$DATA/models/" &> /dev/null
        docker cp "$LEGACY:/Satori/Engine/db/." "$DATA/data/" &> /dev/null
        if has_keys "$DATA/wallet"; then
            echo "  Wallet copied: $(ls "$DATA/wallet" | tr '\n' ' ')"
        fi
    fi
    OLD_NAME="${LEGACY}-old-$(date +%Y%m%d%H%M%S)"
    docker stop "$LEGACY" > /dev/null 2>&1
    docker update --restart=no "$LEGACY" > /dev/null 2>&1
    docker rename "$LEGACY" "$OLD_NAME" || fail "could not rename '$LEGACY'. Your wallet is copied to $DATA/wallet."
    echo "  The old container is stopped and kept as '$OLD_NAME'."
    echo "  Once your neuron is running with your wallet, you can remove it: docker rm $OLD_NAME"
fi

# Start the neuron (pulls the latest image)
echo "Starting the Satori neuron..."
cd "$SATORI_HOME" || fail "could not open $SATORI_HOME"
"$INSTALL_DIR/satori" restart || fail "the neuron did not start. Your files are in $DATA."

echo ""
echo "Satori installed. Your wallet and data are in: $DATA"
echo "Back that folder up; it holds your keys."
echo ""
echo "Run satori commands from $SATORI_HOME:"
echo "  cd $SATORI_HOME && satori restart   # update to the latest version"
echo "  cd $SATORI_HOME && satori logs      # follow the logs"
echo "  cd $SATORI_HOME && satori           # open the wallet CLI"
echo ""
echo "Web UI: http://localhost:24601"
