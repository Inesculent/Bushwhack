"""Backward-compatible wrapper for AACR-only graph-ready generation."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.research_pipeline.constants import TARGET_LANGUAGES
from src.data.research_pipeline.pipeline import run_aacr_only


if __name__ == "__main__":
    df = run_aacr_only(target_languages=TARGET_LANGUAGES)
    print(f"AACR graph-ready rows: {len(df)}")