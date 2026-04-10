from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.config import get_settings

from .constants import (
    AACR_BENCH_CONFIG,
    LOG_DIR,
    PLOTS_DATASET_COMPOSITION_DIR,
    PLOTS_METRIC_DISTRIBUTIONS_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    SWE_PRBENCH_CONFIG,
    TARGET_LANGUAGES,
)
from .github_api import GitHubPullRequestEnricher
from .logging_utils import configure_logger
from .processors import AACRBenchProcessor, SWEPRBenchProcessor
from .utils import ensure_directories
from .visualization import generate_publication_plots


@dataclass(frozen=True, slots=True)
class PipelineResult:
    swe_output_path: Path
    aacr_output_path: Path
    swe_rows: int
    aacr_rows: int


def run_research_pipeline(
    target_languages: tuple[str, ...] = TARGET_LANGUAGES,
    write_raw: bool = True,
    generate_plots: bool = True,
) -> PipelineResult:
    ensure_directories(
        [
            RAW_DIR,
            PROCESSED_DIR,
            PLOTS_DATASET_COMPOSITION_DIR,
            PLOTS_METRIC_DISTRIBUTIONS_DIR,
            LOG_DIR,
        ]
    )

    logger = configure_logger(LOG_DIR / "research_pipeline.log")
    logger.info("Running research pipeline with target languages: %s", target_languages)

    target_language_set = {language.strip().lower() for language in target_languages}
    settings = get_settings()
    enricher = GitHubPullRequestEnricher(logger=logger, token=settings.github_personal_access_token)

    swe_processor = SWEPRBenchProcessor(
        dataset_config=SWE_PRBENCH_CONFIG,
        target_languages=target_language_set,
        logger=logger,
        raw_dir=RAW_DIR,
        enricher=enricher,
        write_raw=write_raw,
    )
    aacr_processor = AACRBenchProcessor(
        dataset_config=AACR_BENCH_CONFIG,
        target_languages=target_language_set,
        logger=logger,
        raw_dir=RAW_DIR,
        enricher=enricher,
        write_raw=write_raw,
    )

    swe_graph_ready = swe_processor.process()
    aacr_graph_ready = aacr_processor.process()

    swe_output_path = PROCESSED_DIR / "swe_prbench_graph_ready.csv"
    aacr_output_path = PROCESSED_DIR / "aacr_bench_graph_ready.csv"

    swe_graph_ready.to_csv(swe_output_path, index=False)
    aacr_graph_ready.to_csv(aacr_output_path, index=False)

    logger.info("Saved SWE-PRBench graph-ready CSV to %s", swe_output_path)
    logger.info("Saved AACR-Bench graph-ready CSV to %s", aacr_output_path)

    if generate_plots:
        generate_publication_plots(
            swe_graph_ready_df=swe_graph_ready,
            aacr_graph_ready_df=aacr_graph_ready,
            composition_dir=PLOTS_DATASET_COMPOSITION_DIR,
            metric_dir=PLOTS_METRIC_DISTRIBUTIONS_DIR,
            logger=logger,
        )

    return PipelineResult(
        swe_output_path=swe_output_path,
        aacr_output_path=aacr_output_path,
        swe_rows=len(swe_graph_ready),
        aacr_rows=len(aacr_graph_ready),
    )


def run_aacr_only(
    target_languages: tuple[str, ...] = TARGET_LANGUAGES,
    write_raw: bool = True,
) -> pd.DataFrame:
    ensure_directories([RAW_DIR, PROCESSED_DIR, LOG_DIR])
    logger = configure_logger(LOG_DIR / "research_pipeline.log")
    settings = get_settings()

    target_language_set = {language.strip().lower() for language in target_languages}
    enricher = GitHubPullRequestEnricher(logger=logger, token=settings.github_personal_access_token)
    processor = AACRBenchProcessor(
        dataset_config=AACR_BENCH_CONFIG,
        target_languages=target_language_set,
        logger=logger,
        raw_dir=RAW_DIR,
        enricher=enricher,
        write_raw=write_raw,
    )
    output = processor.process()
    output_path = PROCESSED_DIR / "aacr_bench_graph_ready.csv"
    output.to_csv(output_path, index=False)
    logger.info("Saved AACR-Bench graph-ready CSV to %s", output_path)
    return output
