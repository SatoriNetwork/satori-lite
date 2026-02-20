#!/bin/bash
#
# Satori-Lite Multi-Architecture Docker Build Script
# ===================================================
# Builds Docker images for both amd64 (Windows/Intel) and arm64 (Apple Silicon)
#
# Usage:
#   ./build.sh                  # Build :latest locally
#   ./build.sh dev              # Build :dev locally
#   ./build.sh push             # Push :latest to Docker Hub
#   ./build.sh push dev         # Push :dev to Docker Hub
#   ./build.sh push latest dev  # Push multiple tags
#   ./build.sh push all         # Push :latest + satorineuron:p2p & :latest
#

set -e

# Configuration
IMAGE_NAME="satorinet/satori-lite"
PLATFORMS="linux/amd64,linux/arm64"
BUILDER_NAME="multiarch"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}  Satori-Lite Multi-Arch Build${NC}"
echo -e "${BLUE}======================================${NC}"

# Ensure buildx builder exists
if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    echo -e "${GREEN}[INFO]${NC} Creating buildx builder '$BUILDER_NAME'..."
    docker buildx create --name "$BUILDER_NAME" --use
else
    docker buildx use "$BUILDER_NAME"
fi

# Parse arguments
PUSH_MODE=false
PUSH_ALL=false
TAGS=""
TAG_LIST=""

if [ "$1" = "push" ]; then
    PUSH_MODE=true
    shift
    # Check for "all" command
    if [ "$1" = "all" ]; then
        PUSH_ALL=true
        shift
    fi
fi

# Get tags from remaining arguments, default to 'latest'
if [ $# -eq 0 ]; then
    TAGS="-t ${IMAGE_NAME}:latest"
    TAG_LIST="latest"
else
    for tag in "$@"; do
        TAGS="$TAGS -t ${IMAGE_NAME}:$tag"
        [ -n "$TAG_LIST" ] && TAG_LIST="$TAG_LIST, $tag" || TAG_LIST="$tag"
    done
fi

if [ "$PUSH_MODE" = true ]; then
    echo -e "${GREEN}[INFO]${NC} Mode: Build and PUSH"
    echo -e "${GREEN}[INFO]${NC} Platforms: $PLATFORMS"
else
    echo -e "${YELLOW}[INFO]${NC} Mode: Local build only"
fi
echo -e "${GREEN}[INFO]${NC} Tags: $TAG_LIST"

# Build (satorilib is provided as a named build context so the Dockerfile can COPY --from=satorilib)
if [ "$PUSH_MODE" = true ]; then
    docker buildx build \
        --platform "$PLATFORMS" \
        --build-context satorilib=../satorilib/src \
        $TAGS \
        --push \
        .
else
    echo -e "${YELLOW}[INFO]${NC} Loading into local Docker (current platform only)..."
    docker buildx build \
        --build-context satorilib=../satorilib/src \
        $TAGS \
        --load \
        .
fi

echo ""
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}  Build Complete!${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""

# Handle "push all" - create satorineuron tags
if [ "$PUSH_ALL" = true ]; then
    echo -e "${BLUE}======================================${NC}"
    echo -e "${BLUE}  Creating satorineuron tags${NC}"
    echo -e "${BLUE}======================================${NC}"
    echo ""

    SOURCE_IMAGE="${IMAGE_NAME}:latest"
    NEURON_IMAGE="satorinet/satorineuron"

    echo -e "${GREEN}[INFO]${NC} Creating ${NEURON_IMAGE}:p2p from ${SOURCE_IMAGE}..."
    docker buildx imagetools create -t "${NEURON_IMAGE}:p2p" "${SOURCE_IMAGE}"

    echo -e "${GREEN}[INFO]${NC} Creating ${NEURON_IMAGE}:latest from ${SOURCE_IMAGE}..."
    docker buildx imagetools create -t "${NEURON_IMAGE}:latest" "${SOURCE_IMAGE}"

    echo ""
    echo -e "${GREEN}======================================${NC}"
    echo -e "${GREEN}  All Images Pushed!${NC}"
    echo -e "${GREEN}======================================${NC}"
    echo ""
    echo "Pushed to Docker Hub:"
    echo "  - ${IMAGE_NAME}:latest"
    echo "  - ${NEURON_IMAGE}:p2p"
    echo "  - ${NEURON_IMAGE}:latest"
    echo ""
    echo "Supported platforms:"
    echo "  - linux/amd64 (Windows, Intel Macs, Linux)"
    echo "  - linux/arm64 (Apple Silicon, ARM servers)"
elif [ "$PUSH_MODE" = true ]; then
    echo "Pushed to Docker Hub:"
    for tag in "$@"; do
        echo "  - ${IMAGE_NAME}:$tag"
    done
    [ $# -eq 0 ] && echo "  - ${IMAGE_NAME}:latest"
    echo ""
    echo "Supported platforms:"
    echo "  - linux/amd64 (Windows, Intel Macs, Linux)"
    echo "  - linux/arm64 (Apple Silicon, ARM servers)"
else
    echo "Built locally: ${IMAGE_NAME}:${TAG_LIST}"
    echo ""
    echo -e "${YELLOW}To push to Docker Hub:${NC}"
    echo "  ./build.sh push             # Push :latest"
    echo "  ./build.sh push dev         # Push :dev"
    echo "  ./build.sh push latest dev  # Push multiple tags"
    echo "  ./build.sh push all         # Push all + satorineuron tags"
fi
