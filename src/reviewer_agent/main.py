"""CLI entry point for parallel reviewer-graph runs."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

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
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    args = parse_args()

    if args.dataset != "aacr":
        raise ValueError(f"Unsupported dataset: {args.dataset}")

    artifacts = run_aacr_reviewer(
        dataset_path=args.dataset_path,
        run_id=args.run_id,
        limit=args.limit,
        output_root=args.output_root,
        repo_root=args.repo_root,
    )
    logger.info("run_id: %s", artifacts.run_id)
    logger.info("output_dir: %s", artifacts.output_dir)
    logger.info("manifest: %s", artifacts.manifest_path)
    logger.info("processed: %s", artifacts.processed)
    logger.info("succeeded: %s", artifacts.succeeded)
    logger.info("failed: %s", artifacts.failed)


if __name__ == "__main__":
    main()
