#!/usr/bin/env bash
# Start loopback Redis for --remote Slurm jobs (no Docker).
set -euo pipefail

PORT="${REVIEW_REDIS_PORT:-6379}"
PIDFILE="${REVIEW_REDIS_PIDFILE:-${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/bw-redis.pid}"
CONF="${REVIEW_REDIS_CONF:-}"

# Explicitly find redis-server in .venv if it was installed there
REDIS_BIN="redis-server"
if [[ -n "${REPO_ROOT:-}" ]] && [[ -x "${REPO_ROOT}/.venv/bin/redis-server" ]]; then
  REDIS_BIN="${REPO_ROOT}/.venv/bin/redis-server"
fi

if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
  echo "Redis already running (pid $(cat "${PIDFILE}"))."
  export REVIEW_REDIS_URL="redis://127.0.0.1:${PORT}/0"
  export REVIEW_REDIS_ENABLED=true
  exit 0
fi

if [[ -n "${CONF}" ]]; then
  "${REDIS_BIN}" "${CONF}" --daemonize yes --pidfile "${PIDFILE}" --port "${PORT}"
else
  "${REDIS_BIN}" \
    --daemonize yes \
    --pidfile "${PIDFILE}" \
    --port "${PORT}" \
    --bind 127.0.0.1 \
    --maxmemory "${REVIEW_REDIS_MAXMEMORY:-8gb}" \
    --maxmemory-policy allkeys-lru
fi

export REVIEW_REDIS_URL="redis://127.0.0.1:${PORT}/0"
export REVIEW_REDIS_ENABLED=true
echo "Redis started on ${REVIEW_REDIS_URL} (pidfile ${PIDFILE})"
