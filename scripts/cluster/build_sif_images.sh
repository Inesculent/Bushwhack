#!/usr/bin/env bash
# Build Apptainer SIF images for --remote / cluster runs.
# Run on a login node where Docker and Apptainer are available.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${1:-${REPO_ROOT}/containers}"
mkdir -p "${OUT_DIR}"

cd "${REPO_ROOT}"

echo "Building Docker images (if missing)..."
docker build -t agent-fs-sandbox:latest -f docker_mcp/fs-mcp/Dockerfile docker_mcp/fs-mcp
docker build -t verifier-test-env:latest -f Dockerfile.verifier .

echo "Converting to SIF under ${OUT_DIR}..."
apptainer build "${OUT_DIR}/agent-fs-sandbox.sif" "docker-daemon://agent-fs-sandbox:latest"
apptainer build "${OUT_DIR}/verifier-test-env.sif" "docker-daemon://verifier-test-env:latest"

echo "Done."
echo "  REVIEW_APPTAINER_IMAGE=${OUT_DIR}/agent-fs-sandbox.sif"
echo "  REVIEW_APPTAINER_VERIFIER_IMAGE=${OUT_DIR}/verifier-test-env.sif"
