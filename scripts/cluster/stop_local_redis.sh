#!/usr/bin/env bash
# Stop loopback Redis started by start_local_redis.sh
set -euo pipefail

PIDFILE="${REVIEW_REDIS_PIDFILE:-${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/bw-redis.pid}"

if [[ -f "${PIDFILE}" ]]; then
  PID="$(cat "${PIDFILE}")"
  if kill -0 "${PID}" 2>/dev/null; then
    kill "${PID}"
    echo "Stopped Redis (pid ${PID})."
  fi
  rm -f "${PIDFILE}"
else
  echo "No Redis pidfile at ${PIDFILE}; nothing to stop."
fi
