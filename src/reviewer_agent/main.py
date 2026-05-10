"""CLI entry point for parallel reviewer-graph runs."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.reviewer_agent.harness.aacr import (
    DEFAULT_AACR_PROCESSED_PATH,
    DatasetRange,
    run_aacr_reviewer,
)

logger = logging.getLogger(__name__)


def _parse_dataset_range(value: str) -> DatasetRange:
    """Parse a 1-based inclusive range like '11:20', '11-', or '11'."""
    raw = value.strip()
    if not raw:
        raise argparse.ArgumentTypeError("range must not be empty")

    separator = ":" if ":" in raw else "-" if "-" in raw else ""
    if not separator:
        try:
            index = int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("range must be START:END, START-, or START") from exc
        dataset_range = DatasetRange(start=index, end=index)
    else:
        start_raw, end_raw = raw.split(separator, maxsplit=1)
        if not start_raw:
            raise argparse.ArgumentTypeError("range start is required")
        try:
            start = int(start_raw)
            end = int(end_raw) if end_raw else None
        except ValueError as exc:
            raise argparse.ArgumentTypeError("range bounds must be integers") from exc
        dataset_range = DatasetRange(start=start, end=end)

    try:
        dataset_range.validate()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return dataset_range


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the parallel reviewer graph over a benchmark dataset."
    )
    parser.add_argument(
        "--dataset",
        choices=["aacr"],
        default="aacr",
        help="Which benchmark harness to use. Only 'aacr' is wired today.",
    )
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=DEFAULT_AACR_PROCESSED_PATH,
        help="Path to the processed dataset CSV.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional run identifier; a short UUID is generated when omitted.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on the number of unique PRs to process.",
    )
    parser.add_argument(
        "--range",
        dest="dataset_range",
        type=_parse_dataset_range,
        default=None,
        help="Optional 1-based inclusive PR range after de-duplication, e.g. '11:20' or '11-'.",
    )
    parser.add_argument(
        "--pr-url",
        default=None,
        help="Optional exact PR URL to run from the processed dataset before applying --limit.",
    )
    parser.add_argument(
        "--snapshot-id",
        default=None,
        help=(
            "Bushwhack snapshot folder name under snapshot_base_path (e.g. bushwhack_runs/<name>). "
            "Loads graph/topology/semantic artifacts and reviews the PR diff without re-running Phase 2."
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Override reviewer_agent_output_dir from settings.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "Local git checkout root (absolute path). Overrides automatic snapshot resume behavior: when "
            "--snapshot-id is used and metadata repo_path is a GitHub URL, the harness may fetch "
            "pull/<PR>/head under <snapshot_root>/_reviewer_worktree using host git if available. "
            "--repo-root skips that and mounts your checkout read-only (recommended when you already "
            "have the PR checked out). Without it, the verifier still runs self-contained generated scripts in "
            "Docker; full in-container clone is opt-in (verifier_clone_remote_in_container + git in the image)."
        ),
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Emit reviewer graph tracing logs for planning, worker dispatch, and synthesis.",
    )
    parser.add_argument(
        "--basic-graph",
        action="store_true",
        help="Use the basic reviewer graph without adversarial critique/reflection nodes.",
    )
    parser.add_argument(
        "--llm-timeout",
        type=int,
        default=None,
        help="Override REVIEW_LOCAL_LLM_TIMEOUT_SECONDS for local Qwen/OpenAI-compatible calls.",
    )
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        default=None,
        help="Override REVIEW_LOCAL_LLM_MAX_RETRIES for local Qwen/OpenAI-compatible calls.",
    )
    parser.add_argument(
        "--keep-redis-checkpoints",
        action="store_true",
        help="Leave reviewer graph Redis checkpoints in place after each PR run.",
    )
    return parser.parse_args()


def _cli_flags_for_run_meta(args: argparse.Namespace) -> dict[str, Any]:
    """Serialize reviewer-agent CLI flags for run_meta.json (JSON-friendly)."""
    return {
        "dataset": args.dataset,
        "dataset_path": str(args.dataset_path),
        "run_id": args.run_id,
        "limit": args.limit,
        "range": (
            {"start": args.dataset_range.start, "end": args.dataset_range.end}
            if args.dataset_range is not None
            else None
        ),
        "pr_url": args.pr_url,
        "snapshot_id": args.snapshot_id,
        "output_root": str(args.output_root) if args.output_root is not None else None,
        "repo_root": str(args.repo_root) if args.repo_root is not None else None,
        "trace": args.trace,
        "basic_graph": args.basic_graph,
        "llm_timeout": args.llm_timeout,
        "llm_max_retries": args.llm_max_retries,
        "keep_redis_checkpoints": args.keep_redis_checkpoints,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    args = parse_args()

    if args.dataset != "aacr":
        raise ValueError(f"Unsupported dataset: {args.dataset}")

    if args.llm_timeout is not None:
        os.environ["REVIEW_LOCAL_LLM_TIMEOUT_SECONDS"] = str(args.llm_timeout)
    if args.llm_max_retries is not None:
        os.environ["REVIEW_LOCAL_LLM_MAX_RETRIES"] = str(args.llm_max_retries)
    if args.keep_redis_checkpoints:
        os.environ["REVIEW_REVIEWER_CLEANUP_REDIS_CHECKPOINTS"] = "false"

    if (
        args.llm_timeout is not None
        or args.llm_max_retries is not None
        or args.keep_redis_checkpoints
    ):
        from src.config import get_settings

        get_settings.cache_clear()

    artifacts = run_aacr_reviewer(
        dataset_path=args.dataset_path,
        run_id=args.run_id,
        limit=args.limit,
        pr_url=args.pr_url,
        dataset_range=args.dataset_range,
        output_root=args.output_root,
        repo_root=args.repo_root,
        trace=args.trace,
        use_basic_graph=args.basic_graph,
        cli_flags=_cli_flags_for_run_meta(args),
        snapshot_id=args.snapshot_id,
    )
    logger.info("run_id: %s", artifacts.run_id)
    logger.info("output_dir: %s", artifacts.output_dir)
    logger.info("manifest: %s", artifacts.manifest_path)
    logger.info("processed: %s", artifacts.processed)
    logger.info("succeeded: %s", artifacts.succeeded)
    logger.info("failed: %s", artifacts.failed)


if __name__ == "__main__":
    main()
