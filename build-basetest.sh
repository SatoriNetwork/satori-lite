#!/bin/bash
#
# Build and push satorinet/satori-lite:base-test (amd64), the tester image
# pinned to the dev central. Run on the build server from satori-lite, with
# satorilib checked out next to it on the same branch.
#
# Usage: ./build-basetest.sh            # build + push
#        ./build-basetest.sh --no-push  # build only
set -e

TAG="satorinet/satori-lite:base-test"
SRC_TAG="satori-lite:base-test-src"

docker buildx build \
    --platform linux/amd64 \
    --build-context satorilib=../satorilib \
    -t "$SRC_TAG" \
    --load \
    .

docker build -f Dockerfile.basetest --build-arg BASE_IMAGE="$SRC_TAG" -t "$TAG" .

if [ "$1" != "--no-push" ]; then
    docker push "$TAG"
fi
echo "built $TAG ($(git rev-parse --short HEAD), satorilib $(git -C ../satorilib rev-parse --short HEAD))"
