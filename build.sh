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
TAGS=""
TAG_LIST=""

if [ "$1" = "push" ]; then
    PUSH_MODE=true
    shift
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

# Build
if [ "$PUSH_MODE" = true ]; then
    docker buildx build \
        --platform "$PLATFORMS" \
        $TAGS \
        --push \
        .
else
    echo -e "${YELLOW}[INFO]${NC} Loading into local Docker (current platform only)..."
    docker buildx build \
        $TAGS \
        --load \
        .
fi

echo ""
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}  Build Complete!${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""
if [ "$PUSH_MODE" = true ]; then
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
fi
