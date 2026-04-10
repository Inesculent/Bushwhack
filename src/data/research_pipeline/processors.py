from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from datasets import load_dataset

from .constants import DatasetConfig
from .github_api import GitHubPullRequestEnricher
from .utils import (
    coalesce_columns,
    is_target_language,
    normalize_language,
    parse_repo_from_pr_url,
    repo_to_url,
    to_numeric,
    write_raw_snapshot,
)


class BaseDatasetProcessor(ABC):
    def __init__(
        self,
        dataset_config: DatasetConfig,
        target_languages: set[str],
        logger: logging.Logger,
        raw_dir: Path,
        write_raw: bool = True,
    ) -> None:
        self._dataset_config = dataset_config
        self._target_languages = target_languages
        self._logger = logger
        self._raw_dir = raw_dir
        self._write_raw = write_raw

    def load(self) -> pd.DataFrame:
        self._logger.info("Loading dataset '%s' (%s)", self._dataset_config.key, self._dataset_config.hf_dataset)
        kwargs: dict[str, object] = {"split": self._dataset_config.split}
        if self._dataset_config.hf_config:
            dataset = load_dataset(self._dataset_config.hf_dataset, self._dataset_config.hf_config, **kwargs)
        else:
            dataset = load_dataset(self._dataset_config.hf_dataset, **kwargs)

        df = dataset.to_pandas()
        self._logger.info("Loaded %s rows for dataset '%s'", len(df), self._dataset_config.key)

        if self._write_raw:
            raw_path = self._raw_dir / f"{self._dataset_config.key}_raw.csv"
            write_raw_snapshot(df, raw_path)
            self._logger.info("Wrote raw dataset snapshot to %s", raw_path)

        return df

    @abstractmethod
    def process(self) -> pd.DataFrame:
        raise NotImplementedError


class SWEPRBenchProcessor(BaseDatasetProcessor):
    def __init__(
        self,
        dataset_config: DatasetConfig,
        target_languages: set[str],
        logger: logging.Logger,
        raw_dir: Path,
        enricher: GitHubPullRequestEnricher | None = None,
        write_raw: bool = True,
    ) -> None:
        super().__init__(dataset_config, target_languages, logger, raw_dir, write_raw=write_raw)
        self._enricher = enricher

    def process(self) -> pd.DataFrame:
        df = self.load().copy()

        language_column = self._dataset_config.language_column
        if language_column not in df.columns:
            raise ValueError(f"Expected language column '{language_column}' not found in SWE-PRBench dataset")

        df = df[df[language_column].apply(lambda x: is_target_language(x, self._target_languages))].copy()
        self._logger.info("SWE-PRBench rows after language filter: %s", len(df))

        df["repo"] = coalesce_columns(df, ["repo", "repository"])
        missing_repo = df["repo"].isna() | (df["repo"].astype(str).str.strip() == "")
        if "pr_url" in df.columns and missing_repo.any():
            df.loc[missing_repo, "repo"] = df.loc[missing_repo, "pr_url"].apply(parse_repo_from_pr_url)
        df["repo"] = df["repo"].astype(str)

        df["repo_url"] = df["repo"].apply(repo_to_url)
        if "pr_url" not in df.columns:
            raise ValueError("SWE-PRBench dataset is missing required 'pr_url' column")

        repo_sizes: dict[str, int] = {}
        repo_structures = {}
        if self._enricher is not None:
            repo_sizes = self._enricher.fetch_repo_sizes(df["repo"].dropna().astype(str).unique().tolist())
            repo_structures = self._enricher.fetch_repo_structure_bulk(df["repo"].dropna().astype(str).unique().tolist())

        repo_total_files_map = {repo: metrics.repo_total_files for repo, metrics in repo_structures.items()}
        repo_python_files_map = {repo: metrics.repo_python_files for repo, metrics in repo_structures.items()}
        repo_total_directories_map = {repo: metrics.repo_total_directories for repo, metrics in repo_structures.items()}
        repo_max_depth_map = {repo: metrics.repo_max_directory_depth for repo, metrics in repo_structures.items()}

        df["base_commit"] = coalesce_columns(df, ["base_commit", "base_sha"])
        df["head_commit"] = coalesce_columns(df, ["head_commit", "head_sha"])

        df["lines_added"] = to_numeric(coalesce_columns(df, ["lines_added", "additions"]))
        df["lines_removed"] = to_numeric(coalesce_columns(df, ["lines_removed", "deletions"]))
        df["total_lines_changed"] = df["lines_added"] + df["lines_removed"]
        df["files_changed"] = to_numeric(coalesce_columns(df, ["files_changed", "changed_files"]))
        df["num_comments"] = to_numeric(coalesce_columns(df, ["num_substantive_comments", "num_comments"]))
        df["repo_size_kb"] = to_numeric(df["repo"].map(repo_sizes), default=0).astype(int)
        df["repo_total_files"] = to_numeric(df["repo"].map(repo_total_files_map), default=0).astype(int)
        df["repo_python_files"] = to_numeric(df["repo"].map(repo_python_files_map), default=0).astype(int)
        df["repo_total_directories"] = to_numeric(df["repo"].map(repo_total_directories_map), default=0).astype(int)
        df["repo_max_directory_depth"] = to_numeric(df["repo"].map(repo_max_depth_map), default=0).astype(int)

        df["target_language"] = coalesce_columns(df, [language_column]).apply(normalize_language)
        df["dataset"] = "swe_prbench"

        return df[
            [
                "dataset",
                "target_language",
                "repo",
                "repo_url",
                "pr_url",
                "base_commit",
                "head_commit",
                "files_changed",
                "lines_added",
                "lines_removed",
                "total_lines_changed",
                "num_comments",
                "repo_size_kb",
                "repo_total_files",
                "repo_python_files",
                "repo_total_directories",
                "repo_max_directory_depth",
            ]
        ].copy()


class AACRBenchProcessor(BaseDatasetProcessor):
    def __init__(
        self,
        dataset_config: DatasetConfig,
        target_languages: set[str],
        logger: logging.Logger,
        raw_dir: Path,
        enricher: GitHubPullRequestEnricher,
        write_raw: bool = True,
    ) -> None:
        super().__init__(dataset_config, target_languages, logger, raw_dir, write_raw=write_raw)
        self._enricher = enricher

    def process(self) -> pd.DataFrame:
        df = self.load().copy()

        language_column = self._dataset_config.language_column
        if language_column not in df.columns:
            raise ValueError(f"Expected language column '{language_column}' not found in AACR-Bench dataset")

        df = df[df[language_column].apply(lambda x: is_target_language(x, self._target_languages))].copy()
        self._logger.info("AACR-Bench rows after language filter: %s", len(df))

        if "pr_url" not in df.columns:
            raise ValueError("AACR-Bench dataset is missing required 'pr_url' column")

        df["repo"] = df["pr_url"].apply(parse_repo_from_pr_url)
        df["repo_url"] = df["repo"].apply(repo_to_url)

        unique_pr_urls = df["pr_url"].dropna().astype(str).unique().tolist()
        self._logger.info("AACR-Bench unique PR URLs for API enrichment: %s", len(unique_pr_urls))

        metrics_rows = [asdict(row) for row in self._enricher.fetch_bulk(unique_pr_urls)]
        metrics_df = pd.DataFrame(metrics_rows)

        if metrics_df.empty:
            self._logger.warning("No GitHub metrics were returned; continuing with dataset-only AACR fields")
            metrics_df = pd.DataFrame(
                columns=[
                    "pr_url",
                    "repo",
                    "pr_files_changed",
                    "pr_lines_added",
                    "pr_lines_removed",
                    "pr_total_lines_changed",
                    "repo_size_kb",
                    "repo_total_files",
                    "repo_python_files",
                    "repo_total_directories",
                    "repo_max_directory_depth",
                ]
            )

        merged = df.merge(metrics_df, how="left", on=["pr_url", "repo"])

        repo_sizes = self._enricher.fetch_repo_sizes(merged["repo"].dropna().astype(str).unique().tolist())
        repo_structures = self._enricher.fetch_repo_structure_bulk(merged["repo"].dropna().astype(str).unique().tolist())
        repo_total_files_map = {repo: metrics.repo_total_files for repo, metrics in repo_structures.items()}
        repo_python_files_map = {repo: metrics.repo_python_files for repo, metrics in repo_structures.items()}
        repo_total_directories_map = {repo: metrics.repo_total_directories for repo, metrics in repo_structures.items()}
        repo_max_depth_map = {repo: metrics.repo_max_directory_depth for repo, metrics in repo_structures.items()}

        merged["repo_size_kb"] = merged["repo_size_kb"].fillna(merged["repo"].map(repo_sizes))
        merged["repo_total_files"] = merged["repo_total_files"].fillna(merged["repo"].map(repo_total_files_map))
        merged["repo_python_files"] = merged["repo_python_files"].fillna(merged["repo"].map(repo_python_files_map))
        merged["repo_total_directories"] = merged["repo_total_directories"].fillna(merged["repo"].map(repo_total_directories_map))
        merged["repo_max_directory_depth"] = merged["repo_max_directory_depth"].fillna(merged["repo"].map(repo_max_depth_map))

        merged["base_commit"] = coalesce_columns(merged, ["pr_target_commit", "base_commit", "base_sha"])
        merged["head_commit"] = coalesce_columns(merged, ["pr_source_commit", "head_commit", "head_sha"])
        merged["path"] = coalesce_columns(merged, ["path", "file_path"])
        merged["from_line"] = to_numeric(coalesce_columns(merged, ["from_line", "line_start"]), default=0).astype(int)
        merged["to_line"] = to_numeric(coalesce_columns(merged, ["to_line", "line_end"]), default=0).astype(int)
        merged["note"] = coalesce_columns(merged, ["note", "comment", "review_comment"], default="")
        merged["category"] = coalesce_columns(merged, ["category", "label"], default="")
        merged["target_language"] = coalesce_columns(merged, [language_column]).apply(normalize_language)
        merged["dataset"] = "aacr_bench"

        merged["pr_files_changed"] = to_numeric(merged["pr_files_changed"], default=0).astype(int)
        merged["pr_lines_added"] = to_numeric(merged["pr_lines_added"], default=0).astype(int)
        merged["pr_lines_removed"] = to_numeric(merged["pr_lines_removed"], default=0).astype(int)
        merged["pr_total_lines_changed"] = to_numeric(merged["pr_total_lines_changed"], default=0).astype(int)
        merged["repo_size_kb"] = to_numeric(merged["repo_size_kb"], default=0).astype(int)
        merged["repo_total_files"] = to_numeric(merged["repo_total_files"], default=0).astype(int)
        merged["repo_python_files"] = to_numeric(merged["repo_python_files"], default=0).astype(int)
        merged["repo_total_directories"] = to_numeric(merged["repo_total_directories"], default=0).astype(int)
        merged["repo_max_directory_depth"] = to_numeric(merged["repo_max_directory_depth"], default=0).astype(int)
        merged["num_comments"] = merged.groupby("pr_url")["pr_url"].transform("count").astype(int)

        return merged[
            [
                "dataset",
                "target_language",
                "repo",
                "repo_url",
                "pr_url",
                "base_commit",
                "head_commit",
                "path",
                "from_line",
                "to_line",
                "note",
                "category",
                "pr_files_changed",
                "pr_lines_added",
                "pr_lines_removed",
                "pr_total_lines_changed",
                "num_comments",
                "repo_size_kb",
                "repo_total_files",
                "repo_python_files",
                "repo_total_directories",
                "repo_max_directory_depth",
            ]
        ].copy()
