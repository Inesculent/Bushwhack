from __future__ import annotations

import logging
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns


def _pr_level_view(
    df: pd.DataFrame,
    total_lines_col: str,
    files_changed_col: str,
    comments_col: str,
    repo_size_col: str | None = None,
    repo_total_files_col: str | None = None,
    repo_python_files_col: str | None = None,
    repo_total_directories_col: str | None = None,
    repo_max_depth_col: str | None = None,
) -> pd.DataFrame:
    agg_map: dict[str, tuple[str, str]] = {
        "repo": ("repo", "first"),
        "total_lines_changed": (total_lines_col, "max"),
        "files_changed": (files_changed_col, "max"),
        "num_comments": (comments_col, "max"),
    }
    if repo_size_col and repo_size_col in df.columns:
        agg_map["repo_size_kb"] = (repo_size_col, "max")
    if repo_total_files_col and repo_total_files_col in df.columns:
        agg_map["repo_total_files"] = (repo_total_files_col, "max")
    if repo_python_files_col and repo_python_files_col in df.columns:
        agg_map["repo_python_files"] = (repo_python_files_col, "max")
    if repo_total_directories_col and repo_total_directories_col in df.columns:
        agg_map["repo_total_directories"] = (repo_total_directories_col, "max")
    if repo_max_depth_col and repo_max_depth_col in df.columns:
        agg_map["repo_max_directory_depth"] = (repo_max_depth_col, "max")

    grouped = df.groupby("pr_url", as_index=False).agg(**agg_map).dropna(subset=["pr_url"])
    return grouped


def _log_bins(series_a: pd.Series, series_b: pd.Series, bins: int = 24) -> np.ndarray:
    positive = pd.concat([series_a, series_b], ignore_index=True)
    positive = positive[positive > 0]
    if positive.empty:
        return np.array([1, 10], dtype=float)
    min_val = max(float(positive.min()), 1.0)
    max_val = max(float(positive.max()), min_val + 1.0)
    return np.logspace(np.log10(min_val), np.log10(max_val), bins)


def plot_mirrored_histograms(swe_pr: pd.DataFrame, aacr_pr: pd.DataFrame, output_path: Path) -> None:
    metrics = [
        ("total_lines_changed", "Lines Changed", True),
        ("files_changed", "Files Changed", True),
        ("num_comments", "Number of Comments", False),
    ]

    if "repo_size_kb" in swe_pr.columns and "repo_size_kb" in aacr_pr.columns:
        swe_repo_size = pd.to_numeric(swe_pr["repo_size_kb"], errors="coerce").dropna()
        aacr_repo_size = pd.to_numeric(aacr_pr["repo_size_kb"], errors="coerce").dropna()
        if (swe_repo_size > 0).any() or (aacr_repo_size > 0).any():
            metrics.append(("repo_size_kb", "Repository Size (KB)", True))

    if "repo_total_files" in swe_pr.columns and "repo_total_files" in aacr_pr.columns:
        swe_total_files = pd.to_numeric(swe_pr["repo_total_files"], errors="coerce").dropna()
        aacr_total_files = pd.to_numeric(aacr_pr["repo_total_files"], errors="coerce").dropna()
        if (swe_total_files > 0).any() or (aacr_total_files > 0).any():
            metrics.append(("repo_total_files", "Repository Total Files", True))

    if "repo_python_files" in swe_pr.columns and "repo_python_files" in aacr_pr.columns:
        swe_python_files = pd.to_numeric(swe_pr["repo_python_files"], errors="coerce").dropna()
        aacr_python_files = pd.to_numeric(aacr_pr["repo_python_files"], errors="coerce").dropna()
        if (swe_python_files > 0).any() or (aacr_python_files > 0).any():
            metrics.append(("repo_python_files", "Repository Python Files", True))

    if "repo_total_directories" in swe_pr.columns and "repo_total_directories" in aacr_pr.columns:
        swe_total_dirs = pd.to_numeric(swe_pr["repo_total_directories"], errors="coerce").dropna()
        aacr_total_dirs = pd.to_numeric(aacr_pr["repo_total_directories"], errors="coerce").dropna()
        if (swe_total_dirs > 0).any() or (aacr_total_dirs > 0).any():
            metrics.append(("repo_total_directories", "Repository Total Directories", True))

    if "repo_max_directory_depth" in swe_pr.columns and "repo_max_directory_depth" in aacr_pr.columns:
        swe_max_depth = pd.to_numeric(swe_pr["repo_max_directory_depth"], errors="coerce").dropna()
        aacr_max_depth = pd.to_numeric(aacr_pr["repo_max_directory_depth"], errors="coerce").dropna()
        if (swe_max_depth > 0).any() or (aacr_max_depth > 0).any():
            metrics.append(("repo_max_directory_depth", "Repository Max Directory Depth", False))

    n_metrics = len(metrics)
    n_cols = min(4, n_metrics)
    n_rows = int(np.ceil(n_metrics / n_cols))

    fig, axes_grid = plt.subplots(n_rows, n_cols, figsize=(5.5 * n_cols, 4.8 * n_rows))
    axes = np.atleast_1d(axes_grid).ravel().tolist()

    for axis, (column, title, use_log_x) in zip(axes, metrics):
        swe_values = pd.to_numeric(swe_pr[column], errors="coerce").dropna()
        aacr_values = pd.to_numeric(aacr_pr[column], errors="coerce").dropna()

        if use_log_x:
            bins = _log_bins(swe_values, aacr_values)
        else:
            max_value = max(float(swe_values.max() if not swe_values.empty else 1), float(aacr_values.max() if not aacr_values.empty else 1))
            bins = np.linspace(0, max_value, 24)

        swe_counts, edges = np.histogram(swe_values, bins=bins)
        aacr_counts, _ = np.histogram(aacr_values, bins=bins)

        centers = (edges[:-1] + edges[1:]) / 2
        heights = np.diff(edges)

        axis.bar(centers, swe_counts, width=heights * 0.9, color="#2E6F95", alpha=0.85, label="SWE-PRBench")
        axis.bar(centers, -aacr_counts, width=heights * 0.9, color="#D67229", alpha=0.85, label="AACR-Bench")
        axis.axhline(0, color="black", linewidth=0.8)
        axis.set_title(title)
        axis.set_xlabel(title, labelpad=10)
        axis.set_ylabel("Frequency (AACR negative, SWE positive)")
        max_count = max(int(swe_counts.max() if len(swe_counts) else 0), int(aacr_counts.max() if len(aacr_counts) else 0), 1)
        axis.set_ylim(-(max_count * 1.15), max_count * 1.15)
        if use_log_x:
            lower = max(float(edges[0]), 1.0)
            upper = max(float(edges[-1]), lower * 1.01)
            lower_decade = 10 ** np.floor(np.log10(lower))
            upper_decade = 10 ** np.ceil(np.log10(upper))
            min_power = int(np.floor(np.log10(lower_decade)))
            max_power = int(np.ceil(np.log10(upper_decade)))
            decade_ticks = [10 ** p for p in range(min_power, max_power + 1)]

            axis.set_xscale("log")
            axis.set_xlim(lower_decade, upper_decade)
            axis.xaxis.set_major_locator(mticker.FixedLocator(decade_ticks))
            axis.set_xticks(decade_ticks, labels=[str(int(tick)) for tick in decade_ticks])
            axis.tick_params(axis="x", which="major", labelsize=11)
            axis.xaxis.set_minor_locator(mticker.NullLocator())
        else:
            axis.set_xlim(float(edges[0]), float(edges[-1]))

    for axis in axes[n_metrics:]:
        axis.set_visible(False)

    handles, labels = axes[0].get_legend_handles_labels()
    fig.suptitle("Mirrored Histograms: Dataset Distribution Comparison", fontsize=14, y=0.98)
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.94), ncol=2, frameon=False)
    fig.tight_layout(rect=[0.0, 0.03, 1.0, 0.90])
    fig.savefig(output_path, dpi=300)
    plt.close(fig)


def plot_context_feasibility(swe_pr: pd.DataFrame, aacr_pr: pd.DataFrame, output_path: Path) -> None:
    thresholds = [2000, 4000, 8000, 16000]

    swe_lines = pd.to_numeric(swe_pr["total_lines_changed"], errors="coerce").dropna()
    aacr_lines = pd.to_numeric(aacr_pr["total_lines_changed"], errors="coerce").dropna()
    all_positive = pd.concat([swe_lines[swe_lines > 0], aacr_lines[aacr_lines > 0]], ignore_index=True)

    if all_positive.empty:
        lower_decade = 1.0
        upper_decade = 10.0
    else:
        lower = max(float(all_positive.min()), 1.0)
        upper = max(float(all_positive.max()), lower * 1.01)
        lower_decade = 10 ** np.floor(np.log10(lower))
        upper_decade = 10 ** np.ceil(np.log10(upper))
        if upper_decade <= lower_decade:
            upper_decade = lower_decade * 10

    min_power = int(np.floor(np.log10(lower_decade)))
    max_power = int(np.ceil(np.log10(upper_decade)))
    decade_ticks = [10 ** p for p in range(min_power, max_power + 1)]
    decade_labels = [str(int(tick)) for tick in decade_ticks]

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), constrained_layout=True)

    sns.histplot(swe_lines[swe_lines > 0], bins=40, log_scale=(True, False), color="#2E6F95", alpha=0.5, ax=axes[0], label="SWE-PRBench")
    sns.histplot(aacr_lines[aacr_lines > 0], bins=40, log_scale=(True, False), color="#D67229", alpha=0.5, ax=axes[0], label="AACR-Bench")
    for threshold in thresholds:
        axes[0].axvline(threshold, color="gray", linestyle="--", linewidth=0.8)
    axes[0].set_title("PR Size Distribution (Lines Changed)")
    axes[0].set_xlabel("Total Lines Changed (log scale)")
    axes[0].set_ylabel("PR Count")
    axes[0].set_xlim(lower_decade, upper_decade)
    axes[0].xaxis.set_major_locator(mticker.FixedLocator(decade_ticks))
    axes[0].set_xticks(decade_ticks, labels=decade_labels)
    axes[0].xaxis.set_minor_locator(mticker.NullLocator())
    axes[0].tick_params(axis="x", which="major", labelsize=10)
    axes[0].legend()

    for values, label, color in [
        (swe_lines[swe_lines > 0], "SWE-PRBench", "#2E6F95"),
        (aacr_lines[aacr_lines > 0], "AACR-Bench", "#D67229"),
    ]:
        values = values.sort_values()
        y = np.arange(1, len(values) + 1) / max(len(values), 1)
        axes[1].plot(values, y, label=label, color=color)

    for threshold in thresholds:
        axes[1].axvline(threshold, color="gray", linestyle="--", linewidth=0.8)

    axes[1].set_xscale("log")
    axes[1].set_xlim(lower_decade, upper_decade)
    axes[1].xaxis.set_major_locator(mticker.FixedLocator(decade_ticks))
    axes[1].set_xticks(decade_ticks, labels=decade_labels)
    axes[1].xaxis.set_minor_locator(mticker.NullLocator())
    axes[1].tick_params(axis="x", which="major", labelsize=10)
    axes[1].set_title("Context Feasibility CDF")
    axes[1].set_xlabel("Total Lines Changed (log scale)")
    axes[1].set_ylabel("Cumulative Share of PRs")
    axes[1].legend()

    fig.savefig(output_path, dpi=300)
    plt.close(fig)


def plot_dataset_composition(swe_pr: pd.DataFrame, aacr_comment: pd.DataFrame, output_path: Path, top_n: int = 12) -> None:
    swe_counts = swe_pr["repo"].value_counts().head(top_n)
    aacr_counts = aacr_comment["repo"].value_counts().head(top_n)

    fig, axes = plt.subplots(1, 2, figsize=(16, 7), constrained_layout=True)

    axes[0].barh(swe_counts.index[::-1], swe_counts.values[::-1], color="#2E6F95")
    axes[0].set_title("SWE-PRBench Top Repositories")
    axes[0].set_xlabel("PR Count")
    axes[0].set_ylabel("Repository")

    axes[1].barh(aacr_counts.index[::-1], aacr_counts.values[::-1], color="#D67229")
    axes[1].set_title("AACR-Bench Top Repositories")
    axes[1].set_xlabel("Comment Count")
    axes[1].set_ylabel("Repository")

    fig.savefig(output_path, dpi=300)
    plt.close(fig)


def generate_publication_plots(
    swe_graph_ready_df: pd.DataFrame,
    aacr_graph_ready_df: pd.DataFrame,
    composition_dir: Path,
    metric_dir: Path,
    logger: logging.Logger,
) -> None:
    sns.set_theme(style="whitegrid", context="paper", font_scale=1.05)

    swe_pr = _pr_level_view(
        swe_graph_ready_df,
        total_lines_col="total_lines_changed",
        files_changed_col="files_changed",
        comments_col="num_comments",
        repo_size_col="repo_size_kb",
        repo_total_files_col="repo_total_files",
        repo_python_files_col="repo_python_files",
        repo_total_directories_col="repo_total_directories",
        repo_max_depth_col="repo_max_directory_depth",
    )
    aacr_pr = _pr_level_view(
        aacr_graph_ready_df,
        total_lines_col="pr_total_lines_changed",
        files_changed_col="pr_files_changed",
        comments_col="num_comments",
        repo_size_col="repo_size_kb",
        repo_total_files_col="repo_total_files",
        repo_python_files_col="repo_python_files",
        repo_total_directories_col="repo_total_directories",
        repo_max_depth_col="repo_max_directory_depth",
    )

    mirrored_hist_path = metric_dir / "mirrored_histograms.png"
    context_path = metric_dir / "context_feasibility.png"
    composition_path = composition_dir / "top_repositories.png"

    plot_mirrored_histograms(swe_pr, aacr_pr, mirrored_hist_path)
    plot_context_feasibility(swe_pr, aacr_pr, context_path)
    plot_dataset_composition(swe_pr, aacr_graph_ready_df, composition_path)

    logger.info("Saved mirrored histograms to %s", mirrored_hist_path)
    logger.info("Saved context feasibility plot to %s", context_path)
    logger.info("Saved dataset composition plot to %s", composition_path)
