#!/usr/bin/env bash
set -euo pipefail
IMAGE_NAME="${IMAGE_NAME:-verifier-test-env:latest}"
echo "Building verifier test environment image: ${IMAGE_NAME}"
docker build \
  --tag "${IMAGE_NAME}" \
  --file Dockerfile.verifier \
  .
echo "Built ${IMAGE_NAME}"
echo ""
echo "Smoke test:"
echo "  docker run --rm ${IMAGE_NAME} python --version"
