#!/usr/bin/env bash
# Shared vLLM startup and shutdown helpers for Slurm launchers.

detect_vllm_gpu_count() {
  local container_path="$1"
  local count=""

  count="$(
    apptainer exec --nv "${container_path}" \
      python3 -c "import torch; print(torch.cuda.device_count())" 2>/dev/null \
      || true
  )"
  count="$(printf '%s' "${count}" | tr -d '[:space:]')"
  if [[ "${count}" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "${count}"
    return 0
  fi

  count="$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l | tr -d '[:space:]')"
  if [[ "${count}" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "${count}"
    return 0
  fi

  printf '0\n'
}

stop_vllm() {
  if [[ -n "${VLLM_PID:-}" ]] && kill -0 "${VLLM_PID}" 2>/dev/null; then
    echo "Stopping vLLM (pid ${VLLM_PID})..."
    kill "${VLLM_PID}" 2>/dev/null || true
    wait "${VLLM_PID}" 2>/dev/null || true
  fi
}

start_and_wait_for_vllm() {
  local model_path="$1"
  local container_path="$2"
  local tensor_parallel_size="${BUSHWHACK_VLLM_TENSOR_PARALLEL_SIZE:-2}"
  local startup_timeout="${BUSHWHACK_VLLM_STARTUP_TIMEOUT_SECONDS:-900}"
  local poll_interval="${BUSHWHACK_VLLM_STARTUP_POLL_SECONDS:-5}"
  local visible_gpu_count
  local deadline
  local exit_code

  visible_gpu_count="$(detect_vllm_gpu_count "${container_path}")"
  echo "=== vLLM allocation diagnostics ==="
  echo "SLURM_JOB_ID=${SLURM_JOB_ID:-unset}"
  echo "SLURM_JOB_GPUS=${SLURM_JOB_GPUS:-unset}"
  echo "SLURM_GPUS_ON_NODE=${SLURM_GPUS_ON_NODE:-unset}"
  echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"
  echo "Visible GPUs inside Apptainer: ${visible_gpu_count}"
  echo "Requested tensor parallel size: ${tensor_parallel_size}"

  if (( visible_gpu_count < tensor_parallel_size )); then
    echo "FATAL: vLLM requires ${tensor_parallel_size} visible GPUs, but Apptainer sees ${visible_gpu_count}." >&2
    echo "The 122B model is not automatically downgraded to one GPU because it may not fit." >&2
    return 1
  fi

  VLLM_LOG_PATH="slurm_logs/vllm-${SLURM_JOB_ID:-local}.log"
  echo "Starting vLLM; log=${VLLM_LOG_PATH}"
  VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 apptainer exec --nv "${container_path}" \
    vllm serve "${model_path}" \
    --host 0.0.0.0 \
    --port 8000 \
    --tensor-parallel-size "${tensor_parallel_size}" \
    --quantization fp8 \
    --kv-cache-dtype fp8 \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.98 \
    --max-model-len 262144 \
    --max-num-seqs 32 \
    --max-num-batched-tokens 8192 \
    --enable-chunked-prefill \
    --trust-remote-code > "${VLLM_LOG_PATH}" 2>&1 &
  VLLM_PID=$!
  trap stop_vllm EXIT

  echo "Waiting up to ${startup_timeout}s for vLLM (pid ${VLLM_PID})..."
  deadline=$((SECONDS + startup_timeout))
  while true; do
    if python3 -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/models', timeout=2).read(1)" \
      >/dev/null 2>&1; then
      echo "vLLM is ready."
      return 0
    fi

    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
      if wait "${VLLM_PID}"; then
        exit_code=0
      else
        exit_code=$?
      fi
      echo "FATAL: vLLM exited before readiness (exit=${exit_code}); inspect ${VLLM_LOG_PATH}." >&2
      return 1
    fi

    if (( SECONDS >= deadline )); then
      echo "FATAL: vLLM did not become ready within ${startup_timeout}s; inspect ${VLLM_LOG_PATH}." >&2
      stop_vllm
      return 1
    fi
    sleep "${poll_interval}"
  done
}
