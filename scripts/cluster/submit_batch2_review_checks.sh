#!/bin/bash
set -euo pipefail

# Current reviewer path is already check-first enforced by default:
# full graph + mental-model planner + mandate explorer + adversarial workers.
# Back-compat: a first positional mode is accepted, but "enforced" is redundant.
ARGS=()
if [[ $# -gt 0 ]]; then
  case "${1}" in
    enforced)
      echo "submit_batch2_review_checks: 'enforced' is the default reviewer path; not forwarding an explicit flag."
      shift
      ;;
    off|log_only)
      echo "submit_batch2_review_checks: forwarding debug override --review-check-mode ${1}."
      ARGS+=("--review-check-mode" "${1}")
      shift
      ;;
  esac
fi
ARGS+=("$@")

sbatch scripts/cluster/run_bushwhack_custom_urls_2.sbatch -- \
  "${ARGS[@]}"
