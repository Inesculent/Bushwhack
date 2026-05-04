"""CLI entry point for parallel reviewer-graph runs."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.reviewer_agent.harness.aacr import DEFAULT_AACR_PROCESSED_PATH, run_aacr_reviewer

logger = logging.getLogger(__name__)


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
        "--output-root",
        type=Path,
        default=None,
        help="Override reviewer_agent_output_dir from settings.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Optional local repository root for direct context smoke runs.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Emit reviewer graph tracing logs for planning, worker dispatch, and synthesis.",
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
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    args = parse_args()

    if args.dataset != "aacr":
        raise ValueError(f"Unsupported dataset: {args.dataset}")

    if args.llm_timeout is not None:
        os.environ["REVIEW_LOCAL_LLM_TIMEOUT_SECONDS"] = str(args.llm_timeout)
    if args.llm_max_retries is not None:
        os.environ["REVIEW_LOCAL_LLM_MAX_RETRIES"] = str(args.llm_max_retries)

    if args.llm_timeout is not None or args.llm_max_retries is not None:
        from src.config import get_settings

        get_settings.cache_clear()

    artifacts = run_aacr_reviewer(
        dataset_path=args.dataset_path,
        run_id=args.run_id,
        limit=args.limit,
        output_root=args.output_root,
        repo_root=args.repo_root,
        trace=args.trace,
    )
    logger.info("run_id: %s", artifacts.run_id)
    logger.info("output_dir: %s", artifacts.output_dir)
    logger.info("manifest: %s", artifacts.manifest_path)
    logger.info("processed: %s", artifacts.processed)
    logger.info("succeeded: %s", artifacts.succeeded)
    logger.info("failed: %s", artifacts.failed)


if __name__ == "__main__":
    main()
