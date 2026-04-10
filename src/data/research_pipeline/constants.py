from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True, slots=True)
class DatasetConfig:
    key: str
    hf_dataset: str
    hf_config: str | None
    split: str
    language_column: str


TARGET_LANGUAGES: Final[tuple[str, ...]] = ("Python",)

DATA_ROOT: Final[Path] = Path("data")
RAW_DIR: Final[Path] = DATA_ROOT / "raw"
PROCESSED_DIR: Final[Path] = DATA_ROOT / "processed"

PLOTS_DIR: Final[Path] = Path("plots")
PLOTS_DATASET_COMPOSITION_DIR: Final[Path] = PLOTS_DIR / "dataset_composition"
PLOTS_METRIC_DISTRIBUTIONS_DIR: Final[Path] = PLOTS_DIR / "metric_distributions"

LOG_DIR: Final[Path] = Path("logs")

SWE_PRBENCH_CONFIG: Final[DatasetConfig] = DatasetConfig(
    key="swe_prbench",
    hf_dataset="foundry-ai/swe-prbench",
    hf_config="prs",
    split="train",
    language_column="language",
)

AACR_BENCH_CONFIG: Final[DatasetConfig] = DatasetConfig(
    key="aacr_bench",
    hf_dataset="Alibaba-Aone/aacr-bench",
    hf_config=None,
    split="train",
    language_column="project_main_language",
)
