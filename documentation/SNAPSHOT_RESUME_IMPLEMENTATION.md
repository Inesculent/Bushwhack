# Snapshot Resume

Snapshot resume lets `reviewer-agent` load an existing exploration snapshot and review one or more PRs without rebuilding Phase 2 semantic context for the same repository.

## Entry Point

```bash
python -m src.reviewer_agent.main --snapshot-id <snapshot-folder-name> [selector flags]
```

Useful selectors:

- `--limit <n>`: process the first `n` de-duplicated PRs from the dataset.
- `--range 1:5`, `--range 11-`, or `--range 7`: process a 1-based inclusive range after de-duplication.
- `--pr-url <url>`: process one exact PR from the dataset.
- `--pr-urls <url> ...`: process an explicit ordered URL list.
- `--repo-root <path>`: use a local checkout instead of letting the harness prepare a host worktree.

Examples:

```bash
# Load snapshot, run on first PR in dataset
python -m src.reviewer_agent.main --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 --limit 1

# Load snapshot, run on a specific PR
python -m src.reviewer_agent.main --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 --pr-url https://github.com/comfyanonymous/ComfyUI/pull/8000

# Load snapshot, run on a range of PRs with trace logging
python -m src.reviewer_agent.main --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 --range 1:5 --trace
```

## Behavior

When `--snapshot-id` is provided:

1. The harness loads snapshot data from `REVIEW_SNAPSHOT_BASE_PATH` / `snapshot_base_path`.
2. For each selected PR, it fetches current PR context and diff from GitHub.
3. It validates that the snapshot repository matches the PR repository.
4. It resolves `repo_path` for on-disk features such as review sandbox reads, verifier bind-mounts, AST, and ripgrep:
   - with `--repo-root`, the provided checkout is used;
   - without `--repo-root`, if snapshot metadata stores an `https://` GitHub URL and host `git` is available, the harness can fetch `pull/<pr>/head` into `<snapshot_root>/_reviewer_worktree`.
5. It builds `GraphState` with the existing snapshot context and the new PR diff.
6. It skips structural extraction and Phase 2 community semantic fan-out for loaded snapshots.
7. It still runs the modern mental-model path: `intent_extractor`, `review_history_context`, `mandate_explorer`, `mandate_patch`, actor-critic planning, `mandate_finalize`, and `snapshot_pin`.
8. It then runs the current check-first review path by default: review-check compiler/validator/context/executor/evidence gate, initial focused context, evidence triage, reflection, optional verifier, critique revision, adjudication, and synthesis.

Skipping Phase 2 does not remove the need for a real repository checkout. Any downstream step that reads files still needs `repo_path` to resolve to a local directory or to a prepared sandbox/worktree.

## Output

Run IDs include the snapshot suffix:

```text
{run_id}:{pr_slug}_from_snapshot_{snapshot_id[:8]}
```

Typical artifacts:

- `raw/{run_id}.json`
- `findings/{run_id}.json`
- `run_meta.json` with `snapshot_loaded: true`
- `manifest.csv`

## Errors

| Scenario | Behavior |
|----------|----------|
| Snapshot not found | Log error and exit for the run. |
| Invalid snapshot JSON | Log error and exit for the run. |
| Snapshot repo does not match PR repo | Per-PR error; skip that PR and continue with others. |
| Empty dataset with `--snapshot-id` | Normal no-work behavior. |

## Verification

```bash
# Check flag appears in help
python -m src.reviewer_agent.main --help

# Verify Phase 2 is skipped with trace logging
python -m src.reviewer_agent.main --snapshot-id <snapshot-id> --limit 1 --trace

# Check node history / raw metadata for the modern path
# Expected nodes include: intent_extractor, review_history_context, mandate_explorer,
# mandate_patch, mandate_finalize, review_check_compiler
```
