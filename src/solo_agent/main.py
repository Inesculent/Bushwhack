"""CLI entry point for the solo-agent one-shot ACR runs."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.solo_agent.harness.aacr import (
    DEFAULT_AACR_PROCESSED_PATH,
    run_aacr_solo,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the solo-agent one-shot ACR worker over a benchmark dataset."
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
        help="Path to the processed dataset CSV (defaults to data/processed/aacr_bench_graph_ready.csv).",
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
        help="Optional cap on the number of unique PRs to process (useful for smoke tests).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Override solo_agent_output_dir from settings.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.dataset == "aacr":
        artifacts = run_aacr_solo(
            dataset_path=args.dataset_path,
            run_id=args.run_id,
            limit=args.limit,
            output_root=args.output_root,
        )
    else:  # defensive; argparse already restricts choices
        raise ValueError(f"Unsupported dataset: {args.dataset}")

    print("run_id:", artifacts.run_id)
    print("output_dir:", artifacts.output_dir)
    print("manifest:", artifacts.manifest_path)
    print("processed:", artifacts.processed)
    print("succeeded:", artifacts.succeeded)
    print("failed:", artifacts.failed)


if __name__ == "__main__":
    main()
