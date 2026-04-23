"""AACR-Bench harness for the solo-agent one-shot ACR worker.

Loads the AACR-Bench processed CSV, deduplicates by ``pr_url``, enriches each
unique PR with title/description/diff via ``GitHubPullRequestEnricher``, invokes
the solo-agent graph once per PR, and persists artifacts under
``<solo_agent_output_dir>/<run_id>/`` so solo-agent experiments never collide
with multi-agent baseline outputs.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional

import pandas as pd

from src.config import get_settings
from src.data.research_pipeline.constants import AACR_BENCH_CONFIG, LOG_DIR, PROCESSED_DIR
from src.data.research_pipeline.github_api import (
    GitHubPullRequestEnricher,
    PullRequestContext,
)
from src.data.research_pipeline.logging_utils import configure_logger
from src.data.research_pipeline.utils import (
    ensure_directories,
    parse_pr_number,
    parse_repo_from_pr_url,
)
from src.domain.state import GraphState
from src.solo_agent.graph import run_solo
from src.solo_agent.prompt import PROMPT_VERSION
from src.solo_agent.worker import EXPERIMENT_TAG

DEFAULT_AACR_PROCESSED_PATH: Path = PROCESSED_DIR / "aacr_bench_graph_ready.csv"


@dataclass(frozen=True, slots=True)
class SoloRunArtifacts:
    run_id: str
    output_dir: Path
    manifest_path: Path
    raw_dir: Path
    findings_dir: Path
    run_meta_path: Path
    processed: int
    succeeded: int
    failed: int


def _slug_for_pr_url(pr_url: str) -> str:
    repo = parse_repo_from_pr_url(pr_url) or "unknown__unknown"
    number = parse_pr_number(pr_url) or 0
    owner, _, name = repo.partition("/")
    return f"{owner or 'unknown'}__{name or 'unknown'}__pr{number}"


def _prepare_output_dirs(output_root: Path, run_id: str) -> tuple[Path, Path, Path]:
    run_dir = output_root / run_id
    raw_dir = run_dir / "raw"
    findings_dir = run_dir / "findings"
    ensure_directories([run_dir, raw_dir, findings_dir])
    return run_dir, raw_dir, findings_dir


def _write_raw(raw_dir: Path, slug: str, raw_text: str) -> Path:
    path = raw_dir / f"{slug}.txt"
    path.write_text(raw_text or "", encoding="utf-8")
    return path


def _write_findings(findings_dir: Path, slug: str, findings: Iterable[Any]) -> Path:
    path = findings_dir / f"{slug}.json"
    payload = [finding.model_dump() for finding in findings]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _load_pr_urls(
    source: pd.DataFrame | Path,
    limit: Optional[int],
    logger: logging.Logger,
) -> List[str]:
    if isinstance(source, pd.DataFrame):
        df = source
    else:
        logger.info("Reading AACR-Bench processed dataset from %s", source)
        df = pd.read_csv(source)

    if "pr_url" not in df.columns:
        raise ValueError("AACR-Bench dataframe is missing required 'pr_url' column")

    urls = (
        df["pr_url"]
        .dropna()
        .astype(str)
        .map(str.strip)
        .loc[lambda s: s != ""]
        .drop_duplicates()
        .tolist()
    )
    if limit is not None and limit > 0:
        urls = urls[:limit]
    return urls


def _invoke_for_pr(
    run_id: str,
    pr_url: str,
    context: PullRequestContext,
) -> dict[str, Any]:
    initial_state: GraphState = {
        "run_id": f"{run_id}:{_slug_for_pr_url(pr_url)}",
        "repo_path": f"https://github.com/{context.repo}",
        "git_diff": context.unified_diff,
        "user_goals": "",
        "global_insights": [],
        "findings": [],
        "token_usage": 0,
        "node_history": [],
        "metadata": {
            "experiment": EXPERIMENT_TAG,
            "pr_url": pr_url,
            "pr_title": context.title,
            "pr_description": context.body,
            "pr_repo": context.repo,
            "pr_number": context.number,
        },
    }
    return run_solo(initial_state)


def run_aacr_solo(
    dataset_path: Path = DEFAULT_AACR_PROCESSED_PATH,
    run_id: Optional[str] = None,
    limit: Optional[int] = None,
    output_root: Optional[Path] = None,
) -> SoloRunArtifacts:
    settings = get_settings()
    ensure_directories([LOG_DIR])
    logger = configure_logger(LOG_DIR / "solo_agent_aacr.log")

    resolved_run_id = run_id or uuid.uuid4().hex[:12]
    resolved_output_root = output_root or settings.solo_agent_output_dir
    run_dir, raw_dir, findings_dir = _prepare_output_dirs(
        Path(resolved_output_root), resolved_run_id
    )

    logger.info(
        "Starting solo-agent AACR run run_id=%s dataset=%s output=%s",
        resolved_run_id,
        dataset_path,
        run_dir,
    )

    pr_urls = _load_pr_urls(dataset_path, limit=limit, logger=logger)
    logger.info("Solo-agent AACR run will process %s unique PR URLs", len(pr_urls))

    enricher = GitHubPullRequestEnricher(
        logger=logger, token=settings.github_personal_access_token
    )

    manifest_rows: List[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    run_started = time.perf_counter()

    for idx, pr_url in enumerate(pr_urls, start=1):
        slug = _slug_for_pr_url(pr_url)
        row: dict[str, Any] = {
            "pr_url": pr_url,
            "slug": slug,
            "status": "pending",
            "raw_path": "",
            "findings_path": "",
            "finding_count": 0,
            "elapsed_ms": 0,
            "error": "",
        }

        context = enricher.fetch_pr_context(pr_url)
        if context is None:
            row["status"] = "skipped_enrichment_failed"
            row["error"] = "github_pr_context_unavailable"
            manifest_rows.append(row)
            failed += 1
            logger.warning("[%s/%s] Skipping %s: enrichment failed", idx, len(pr_urls), pr_url)
            continue

        started = time.perf_counter()
        try:
            result = _invoke_for_pr(run_id=resolved_run_id, pr_url=pr_url, context=context)
        except Exception as exc:  # noqa: BLE001 - per-PR isolation; harness continues
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            row["status"] = "error"
            row["elapsed_ms"] = elapsed_ms
            row["error"] = f"{exc.__class__.__name__}: {exc}"
            manifest_rows.append(row)
            failed += 1
            logger.exception("[%s/%s] Solo-agent run failed for %s", idx, len(pr_urls), pr_url)
            continue

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        metadata = result.get("metadata", {}) or {}
        raw_text = str(metadata.get("solo_agent_raw_response", ""))
        findings = result.get("findings", []) or []

        raw_path = _write_raw(raw_dir, slug, raw_text)
        findings_path = _write_findings(findings_dir, slug, findings)

        row["status"] = "ok"
        row["raw_path"] = str(raw_path.relative_to(run_dir))
        row["findings_path"] = str(findings_path.relative_to(run_dir))
        row["finding_count"] = len(findings)
        row["elapsed_ms"] = elapsed_ms
        manifest_rows.append(row)
        succeeded += 1

        logger.info(
            "[%s/%s] %s ok findings=%s elapsed_ms=%s",
            idx,
            len(pr_urls),
            slug,
            len(findings),
            elapsed_ms,
        )

    manifest_df = pd.DataFrame(manifest_rows)
    manifest_path = run_dir / "manifest.csv"
    manifest_df.to_csv(manifest_path, index=False)

    run_meta_path = run_dir / "run_meta.json"
    run_meta = {
        "experiment": EXPERIMENT_TAG,
        "run_id": resolved_run_id,
        "dataset": AACR_BENCH_CONFIG.key,
        "dataset_path": str(dataset_path),
        "prompt_version": PROMPT_VERSION,
        "model_key": settings.solo_agent_model_key,
        "max_diff_chars": settings.solo_agent_max_diff_chars,
        "total_prs": len(pr_urls),
        "succeeded": succeeded,
        "failed": failed,
        "elapsed_ms": int((time.perf_counter() - run_started) * 1000),
    }
    run_meta_path.write_text(json.dumps(run_meta, indent=2), encoding="utf-8")

    logger.info(
        "Finished solo-agent AACR run run_id=%s succeeded=%s failed=%s elapsed_ms=%s",
        resolved_run_id,
        succeeded,
        failed,
        run_meta["elapsed_ms"],
    )

    return SoloRunArtifacts(
        run_id=resolved_run_id,
        output_dir=run_dir,
        manifest_path=manifest_path,
        raw_dir=raw_dir,
        findings_dir=findings_dir,
        run_meta_path=run_meta_path,
        processed=len(pr_urls),
        succeeded=succeeded,
        failed=failed,
    )
