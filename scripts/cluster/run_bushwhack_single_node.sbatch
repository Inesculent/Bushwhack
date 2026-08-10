#!/bin/bash
#SBATCH --job-name=vllm_qwen
#SBATCH --account=ayadavally
#SBATCH --qos=normal
#SBATCH --gres=gpu:2
#SBATCH -C h100
#SBATCH --nodes=1
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --output=slurm_logs/bushwhack-%j.out
#SBATCH --error=slurm_logs/bushwhack-%j.err
#SBATCH --exclude=evc33


set -euo pipefail

REPO_ROOT="${BUSHWHACK_REPO:-${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}"
cd "${REPO_ROOT}"
mkdir -p slurm_logs

echo "Resolved repository root: ${REPO_ROOT}"
echo "Resolved working directory: $(pwd -P)"
if git -C "${REPO_ROOT}" rev-parse HEAD >/dev/null 2>&1; then
  echo "Git commit: $(git -C "${REPO_ROOT}" rev-parse HEAD)"
  if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain)" ]]; then
    echo "Git working tree dirty: true"
  else
    echo "Git working tree dirty: false"
  fi
else
  echo "Git metadata: unavailable (source fingerprint will still be recorded)"
fi

# ==========================================
# GPU DIAGNOSTICS & LOGGING
# ==========================================
echo "=========================================="
nvidia-smi -L || true
HOST_IP=$(hostname -I | awk '{print $1}')
echo "Base URL: http://$HOST_IP:8000/v1"
echo "=========================================="

# 1. LOAD THE REQUIRED MODULES ON THE COMPUTE NODE
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true

# Python environment diagnostics and activation
echo "=== Python Environment Diagnostics ==="
echo "Default python path: $(which python || echo 'not found')"
echo "Default python version: $(python --version 2>&1 || echo 'N/A')"
echo "Default python3 path: $(which python3 || echo 'not found')"
echo "Default python3 version: $(python3 --version 2>&1 || echo 'N/A')"

CONDA_ACTIVATED=false
# 1. Try to activate Conda environment 'bushwhack_env'
for conda_path in "$HOME/miniforge3" "$HOME/mambaforge" "$HOME/miniconda3" "$HOME/anaconda3"; do
  if [[ -f "${conda_path}/etc/profile.d/conda.sh" ]]; then
    echo "Found Conda installation at ${conda_path}, activating 'bushwhack_env'..."
    source "${conda_path}/etc/profile.d/conda.sh"
    conda activate bushwhack_env
    CONDA_ACTIVATED=true
    break
  fi
done

if [[ "${CONDA_ACTIVATED}" == "false" ]] && command -v conda &> /dev/null; then
  echo "Found conda in PATH, activating 'bushwhack_env'..."
  source "$(conda info --base)/etc/profile.d/conda.sh"
  conda activate bushwhack_env
  CONDA_ACTIVATED=true
fi

# 2. Fallback to virtualenv if Conda is not activated
if [[ "${CONDA_ACTIVATED}" == "false" ]]; then
  if [[ -f "${REPO_ROOT}/.venv/bin/activate" ]]; then
    echo "Found virtual environment at ${REPO_ROOT}/.venv, activating..."
    # shellcheck source=/dev/null
    source "${REPO_ROOT}/.venv/bin/activate"
  else
    echo "No virtual environment or Conda environment activated."
    if [[ -f "${REPO_ROOT}/.venv/Scripts/activate" ]]; then
      echo "WARNING: Found Windows-style virtual environment in .venv!"
      echo "Activator scripts for Windows (.venv/Scripts/activate) will NOT work on Linux/HPC."
    fi
  fi
fi

echo "Activated python path: $(which python || echo 'not found')"
echo "Activated python version: $(python --version 2>&1 || echo 'N/A')"
echo "Activated python3 path: $(which python3 || echo 'not found')"
echo "Activated python3 version: $(python3 --version 2>&1 || echo 'N/A')"
echo "======================================"

# Redis checkpointing is disabled for this test run.
export REVIEW_REDIS_ENABLED="false"

# 2. Path to your Qwen weights and offline container
MODEL_PATH="/lustre/fs1/home/dy828490/bushwhack_dev/qwen-3.5-122b/"
CONTAINER_PATH="/lustre/fs1/home/dy828490/bushwhack_dev/vllm-v0.20.1.sif"

# 3. Mount the Lustre drive into the container
export APPTAINER_BIND="/lustre"

# Validate the allocation, start vLLM, and fail fast if startup dies or times out.
# shellcheck source=vllm_runtime.sh
source "${REPO_ROOT}/vllm_runtime.sh"
start_and_wait_for_vllm "${MODEL_PATH}" "${CONTAINER_PATH}"

# Strip leading "--" if passed by sbatch to separate script arguments
if [ "${1:-}" = "--" ]; then
  shift
fi

# Run the reviewer agent, defaulting to a smoke test if no arguments are provided
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then
  ARGS=("--limit" "1" "--trace")
fi

echo "Reviewer mode: full graph, mental-model planner, mandate explorer, adversarial workers, check-first default"
echo "Reviewer args: --remote ${ARGS[*]}"

PYTHONUNBUFFERED=1 python3 -u src/reviewer_agent/main.py --remote "${ARGS[@]}"
