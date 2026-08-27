#!/usr/bin/env bash
# Builds the three custom images (backend, frontend, simulator) and pushes
# them to docker.io/mescalo, using the exact image names/tags referenced by
# the manifests in k8s/. docker-compose.yml is untouched and keeps building
# these locally from source for local dev - this script is only for
# publishing images the k8s manifests can pull.
#
# Requires: `docker login docker.io` already done (not handled here).
set -euo pipefail

REGISTRY="docker.io/mescalo"
TAG="${TAG:-latest}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for name in backend frontend simulator; do
    image="$REGISTRY/network-control-room-$name:$TAG"
    echo "==> Building $image"
    docker build -t "$image" "$REPO_ROOT/$name" --platform=linux/AMD64
    echo "==> Pushing $image"
    docker push "$image"
done
