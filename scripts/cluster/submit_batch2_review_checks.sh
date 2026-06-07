#!/bin/bash
set -euo pipefail

# Assumption: "review check mode on" means the check-first path should run,
# so default to the fully enabled mode. Override with the first arg if needed.
MODE="${1:-enforced}"

case "${MODE}" in
  off|log_only|enforced)
    ;;
  *)
    echo "Expected review-check mode to be one of: off, log_only, enforced" >&2
    exit 1
    ;;
esac

if [[ $# -gt 0 ]]; then
  shift
fi

sbatch scripts/cluster/run_bushwhack_custom_urls_2.sbatch -- \
  --review-check-mode "${MODE}" \
  "$@"
