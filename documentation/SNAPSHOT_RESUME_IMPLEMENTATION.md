# Snapshot Resume Feature Implementation Summary

## Overview
Added `--snapshot-id` flag to `src/reviewer_agent/main.py` to load exploration snapshots and skip Phase 2 semantic enrichment, allowing reviewers to run on new PRs using existing code analysis.

## Changes Made

### 1. `src/reviewer_agent/main.py`
- Added `--snapshot-id` argument (line ~103)
- Updated `_cli_flags_for_run_meta()` to include `snapshot_id`
- Passed `snapshot_id=args.snapshot_id` to `run_aacr_reviewer()`

### 2. `src/reviewer_agent/harness/aacr.py`
- Added import: `from src.infrastructure.snapshot_loader import SnapshotLoader`
- Added `_load_snapshot_for_resume()` helper function to load snapshot data
- Updated `_invoke_for_pr()` to:
  - Accept `snapshot_data` parameter
  - Inject snapshot data into GraphState when provided
  - Generate run_id with `_from_snapshot_{id[:8]}` suffix
  - Add trace logging when snapshot is loaded
- Updated `run_aacr_reviewer()` to:
  - Accept `snapshot_id` parameter
  - Load snapshot once before the PR loop
  - Validate repo match per-PR (error if mismatch)
  - Pass `snapshot_data` to `_invoke_for_pr()`

### 3. `src/infrastructure/snapshot_loader.py`
- Fixed `load_snapshot_pointer()` to correctly extract `exploration_snapshot` from nested JSON structure

## Usage Examples

```bash
# Load snapshot, run on first PR in dataset
python -m src.reviewer_agent.main --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 --limit 1

# Load snapshot, run on specific PR
python -m src.reviewer_agent.main --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 --pr-url https://github.com/comfyanonymous/ComfyUI/pull/8000

# Load snapshot, run on range of PRs with trace logging
python -m src.reviewer_agent.main --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 --range 1:5 --trace
```

## Behavior

### When `--snapshot-id` is provided:
1. Load snapshot data (graph, topology, community summaries, global summary)
2. For each PR in dataset:
   - Fetch PR context from GitHub API (including diff)
   - Validate snapshot repo matches PR repo (error if mismatch)
   - Construct GraphState with:
     - Existing Phase 2 outputs from snapshot
     - NEW diff from current PR
     - NEW run_id with `_from_snapshot_{id[:8]}` suffix
   - Skip Phase 2 (routing sees `snapshot_root`)
   - Run review planning + adversarial loop

### Output Structure:
- Run ID format: `{run_id}:{pr_slug}_from_snapshot_{snapshot_id[:8]}`
- Example: `abc123:comfyanonymous__ComfyUI__pr7952_from_snapshot_55d7a9ed`
- Output files (unchanged structure):
  - `output/{run_id}/raw/{run_id}.json`
  - `output/{run_id}/findings/{run_id}.json`
  - `output/{run_id}/run_meta.json` (includes `snapshot_loaded: true`)
  - `logs/reviewer_agent_aacr.log`

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Snapshot not found | `sys.exit(1)` with error log |
| Invalid snapshot JSON | `sys.exit(1)` with error log |
| Snapshot repo ≠ PR repo | Per-PR error, skip that PR, continue with others |
| Empty dataset with `--snapshot-id` | Normal behavior (no PRs to process) |

## Verification

```bash
# Check flag appears in help
python -m src.reviewer_agent.main --help

# Test with existing snapshot (will fail without GitHub token, but should load snapshot)
python -m src.reviewer_agent.main --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 --limit 1

# Verify Phase 2 is skipped with trace logging
python -m src.reviewer_agent.main --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 --limit 1 --trace
# Check logs for: route=review_planner (not semantic_dispatch)
```

## Files Modified
1. `src/reviewer_agent/main.py` - Added `--snapshot-id` flag
2. `src/reviewer_agent/harness/aacr.py` - Added snapshot loading logic
3. `src/infrastructure/snapshot_loader.py` - Fixed nested JSON parsing

## Files NOT Modified (already supported this)
- `src/orchestration/nodes/exploration/phase2_routing.py` - Already skips Phase 2 when `snapshot_root` exists
- `src/domain/state.py` - Already has `snapshot_source` field
- `src/infrastructure/snapshot_loader.py` - All methods existed (just fixed a bug)
