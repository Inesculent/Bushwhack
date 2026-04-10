from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.research_pipeline.constants import TARGET_LANGUAGES
from src.data.research_pipeline.pipeline import run_research_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the modular research pipeline for SWE-PRBench and AACR-Bench.")
    parser.add_argument(
        "--target-languages",
        nargs="+",
        default=list(TARGET_LANGUAGES),
        help="Target language filter (e.g., Python).",
    )
    parser.add_argument(
        "--skip-plots",
        action="store_true",
        help="Skip plot generation and only produce processed CSVs.",
    )
    parser.add_argument(
        "--no-raw-dump",
        action="store_true",
        help="Skip writing raw dataset snapshots to data/raw.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    result = run_research_pipeline(
        target_languages=tuple(args.target_languages),
        write_raw=not args.no_raw_dump,
        generate_plots=not args.skip_plots,
    )
    print(f"SWE rows: {result.swe_rows} -> {result.swe_output_path}")
    print(f"AACR rows: {result.aacr_rows} -> {result.aacr_output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
