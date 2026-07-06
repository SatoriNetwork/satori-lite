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
#   ./build.sh push all         # Push :latest + :slim + satorineuron:p2p & :latest
#
# Environment overrides:
#   NO_CACHE=1         Force a full rebuild (--no-cache)
#   PLATFORMS=...      Target platforms (default: linux/amd64,linux/arm64)
#   STRFRY_JOBS=N      Parallel jobs for the strfry C++ compile
#                      (default: nproc capped at 4 - the cap keeps peak RAM
#                      sane on 8GB Docker Desktop laptops)
#   REGISTRY_CACHE=0   Disable the Docker Hub layer cache (push mode only)
#

set -e

# Configuration
IMAGE_NAME="satorinet/satori-lite"
NEURON_IMAGE="satorinet/satorineuron"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
BUILDER_NAME="multiarch"

# Parallel jobs for the strfry compile (see Dockerfile ARG STRFRY_MAKE_JOBS).
DETECTED_CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)
[ "$DETECTED_CORES" -gt 4 ] && DETECTED_CORES=4
STRFRY_JOBS="${STRFRY_JOBS:-$DETECTED_CORES}"
BUILD_ARGS="--build-arg STRFRY_MAKE_JOBS=$STRFRY_JOBS"

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
HAS_LATEST=false

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
    HAS_LATEST=true
else
    for tag in "$@"; do
        TAGS="$TAGS -t ${IMAGE_NAME}:$tag"
        [ -n "$TAG_LIST" ] && TAG_LIST="$TAG_LIST, $tag" || TAG_LIST="$tag"
        [ "$tag" = "latest" ] && HAS_LATEST=true
    done
fi

# "push all" publishes satorineuron:p2p/:latest as mirrors of satori-lite:latest.
# Tag them directly on the main build - one push publishes every repo at once,
# instead of a separate imagetools copy that re-uploads all blobs cross-repo
# (measured at ~14 min). Force :latest into the tag set so the neuron tags
# always mirror THIS build, never a stale :latest already on Docker Hub.
if [ "$PUSH_ALL" = true ]; then
    if [ "$HAS_LATEST" = false ]; then
        TAGS="$TAGS -t ${IMAGE_NAME}:latest"
        TAG_LIST="$TAG_LIST, latest"
    fi
    TAGS="$TAGS -t ${NEURON_IMAGE}:p2p -t ${NEURON_IMAGE}:latest"
    TAG_LIST="$TAG_LIST, satorineuron:p2p, satorineuron:latest"
fi

if [ "$PUSH_MODE" = true ]; then
    echo -e "${GREEN}[INFO]${NC} Mode: Build and PUSH"
    echo -e "${GREEN}[INFO]${NC} Platforms: $PLATFORMS"
else
    echo -e "${YELLOW}[INFO]${NC} Mode: Local build only"
fi
echo -e "${GREEN}[INFO]${NC} Tags: $TAG_LIST"
echo -e "${GREEN}[INFO]${NC} strfry compile jobs: $STRFRY_JOBS"

# Build (satorilib is provided as a named build context so the Dockerfile can COPY --from=satorilib)
CACHE_ARG=""
[ "${NO_CACHE:-0}" = "1" ] && CACHE_ARG="--no-cache"

# Registry-backed layer cache (push mode only). Survives builder recreation /
# machine reboots, which wipe the local buildx cache and otherwise force a
# full multi-hour arm64 recompile. Kept in :buildcache* tags on Docker Hub;
# ignore-error so a cache-export hiccup never fails the image push itself.
CACHE_FROM_MAIN=""; CACHE_TO_MAIN=""
CACHE_FROM_SLIM=""; CACHE_TO_SLIM=""
if [ "$PUSH_MODE" = true ] && [ "${REGISTRY_CACHE:-1}" = "1" ]; then
    CACHE_FROM_MAIN="--cache-from type=registry,ref=${IMAGE_NAME}:buildcache"
    CACHE_TO_MAIN="--cache-to type=registry,ref=${IMAGE_NAME}:buildcache,mode=max,ignore-error=true"
    CACHE_FROM_SLIM="--cache-from type=registry,ref=${IMAGE_NAME}:buildcache-slim --cache-from type=registry,ref=${IMAGE_NAME}:buildcache"
    CACHE_TO_SLIM="--cache-to type=registry,ref=${IMAGE_NAME}:buildcache-slim,mode=max,ignore-error=true"
fi

if [ "$PUSH_MODE" = true ]; then
    docker buildx build \
        --platform "$PLATFORMS" \
        --build-context satorilib=../satorilib \
        $BUILD_ARGS \
        $CACHE_ARG \
        $CACHE_FROM_MAIN \
        $CACHE_TO_MAIN \
        $TAGS \
        --push \
        .
else
    echo -e "${YELLOW}[INFO]${NC} Loading into local Docker (current platform only)..."
    docker buildx build \
        --build-context satorilib=../satorilib \
        $BUILD_ARGS \
        $CACHE_ARG \
        $TAGS \
        --load \
        .
fi

echo ""
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}  Build Complete!${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""

# Handle "push all" - the satorineuron tags were already pushed with the main
# build above; only the slim variant remains.
if [ "$PUSH_ALL" = true ]; then
    echo -e "${BLUE}======================================${NC}"
    echo -e "${BLUE}  Building and pushing slim variant${NC}"
    echo -e "${BLUE}======================================${NC}"
    echo ""

    docker buildx build \
        --platform "$PLATFORMS" \
        --build-context satorilib=../satorilib \
        $BUILD_ARGS \
        $CACHE_ARG \
        $CACHE_FROM_SLIM \
        $CACHE_TO_SLIM \
        -f Dockerfile.slim \
        -t "${IMAGE_NAME}:slim" \
        --push \
        .

    echo ""
    echo -e "${GREEN}======================================${NC}"
    echo -e "${GREEN}  All Images Pushed!${NC}"
    echo -e "${GREEN}======================================${NC}"
    echo ""
    echo "Pushed to Docker Hub:"
    echo "  - ${IMAGE_NAME}:latest"
    echo "  - ${IMAGE_NAME}:slim"
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
