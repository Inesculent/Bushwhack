"""Backward-compatible plotting wrapper; delegates to modular pipeline plots."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.research_pipeline.pipeline import run_research_pipeline


if __name__ == "__main__":
    run_research_pipeline(generate_plots=True, write_raw=False)
    print("Plots generated under plots/dataset_composition and plots/metric_distributions")