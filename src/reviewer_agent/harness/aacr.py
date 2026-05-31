"""AACR-Bench harness for the parallel reviewer graph."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

from src.config import get_settings
from src.data.research_pipeline.constants import AACR_BENCH_CONFIG, LOG_DIR, PROCESSED_DIR
from src.data.research_pipeline.github_api import GitHubPullRequestEnricher, PullRequestContext
from src.data.research_pipeline.logging_utils import configure_logger
from src.data.research_pipeline.utils import ensure_directories, parse_pr_number, parse_repo_from_pr_url
from src.domain.schemas import PreflightSummary
from src.domain.state import GraphState
from src.infrastructure.mcp.client import MCPClient
from src.infrastructure.redis_checkpoint import delete_checkpoint_thread
from src.infrastructure.snapshot_loader import SnapshotLoader
from src.orchestration.reviewer_graph import run_reviewer
from src.orchestration.reviewer_graph_basic import run_reviewer_basic

DEFAULT_AACR_PROCESSED_PATH: Path = PROCESSED_DIR / "aacr_bench_graph_ready.csv"
EXPERIMENT_TAG = "reviewer_graph_parallel"
BASIC_EXPERIMENT_TAG = "reviewer_graph_basic"
DEFAULT_POSITIVE_SAMPLES_PATH = Path(__file__).resolve().parents[3] / "documentation" / "dataset" / "positive_samples.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ReviewerRunArtifacts:
    run_id: str
    output_dir: Path
    manifest_path: Path
    raw_dir: Path
    findings_dir: Path
    run_meta_path: Path
    processed: int
    succeeded: int
    failed: int


@dataclass(frozen=True, slots=True)
class DatasetRange:
    """1-based inclusive dataset row range after PR URL de-duplication."""

    start: int
    end: Optional[int] = None

    def validate(self) -> None:
        if self.start < 1:
            raise ValueError("Dataset range start must be >= 1.")
        if self.end is not None and self.end < self.start:
            raise ValueError("Dataset range end must be >= start.")


def _slug_for_pr_url(pr_url: str) -> str:
    repo = parse_repo_from_pr_url(pr_url) or "unknown__unknown"
    number = parse_pr_number(pr_url) or 0
    owner, _, name = repo.partition("/")
    return f"{owner or 'unknown'}__{name or 'unknown'}__pr{number}"


def _graph_thread_id(run_id: str, pr_url: str, snapshot_data: Optional[Dict[str, Any]]) -> str:
    """LangGraph / Redis thread id for this PR (matches checkpoint cleanup keys)."""
    slug = _slug_for_pr_url(pr_url)
    if snapshot_data:
        sid = snapshot_data["snapshot_id"]
        short = sid[:8] if len(sid) >= 8 else sid
        return f"{run_id}:{slug}_from_snapshot_{short}"
    return f"{run_id}:{slug}"


def _prepare_output_dirs(output_root: Path, run_id: str) -> tuple[Path, Path, Path]:
    run_dir = output_root / run_id
    raw_dir = run_dir / "raw"
    findings_dir = run_dir / "findings"
    ensure_directories([run_dir, raw_dir, findings_dir])
    return run_dir, raw_dir, findings_dir


def _write_raw(raw_dir: Path, slug: str, result: dict[str, Any]) -> Path:
    path = raw_dir / f"{slug}.json"
    metadata = result.get("metadata", {}) or {}
    payload = {
        "metadata": metadata,
        "node_history": result.get("node_history", []),
        "llm_trace": result.get("llm_trace", []),
        "worker_reports": [report.model_dump() for report in result.get("reviewer_worker_reports", []) or []],
        "candidate_findings": [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in result.get("candidate_findings", []) or []
        ],
        "reflection_reports": [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in result.get("reflection_reports", []) or []
        ],
        "focused_context_requests": [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in result.get("focused_context_requests", []) or []
        ],
        "focused_context_results": {
            key: (val.model_dump() if hasattr(val, "model_dump") else val)
            for key, val in (result.get("focused_context_results", {}) or {}).items()
        },
        "review_checks": [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in result.get("review_checks", []) or []
        ],
        "invalid_review_checks": [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in result.get("invalid_review_checks", []) or []
        ],
        "review_check_results": [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in result.get("review_check_results", []) or []
        ],
        "critique_revision_digests": {
            key: (val.model_dump() if hasattr(val, "model_dump") else val)
            for key, val in (result.get("critique_revision_digests", {}) or {}).items()
        },
        "verifier_reports": [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in result.get("verifier_reports", []) or []
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _write_findings(findings_dir: Path, slug: str, findings: Iterable[Any]) -> Path:
    path = findings_dir / f"{slug}.json"
    payload = [finding.model_dump() for finding in findings]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _write_manifest(manifest_path: Path, rows: List[dict[str, Any]]) -> pd.DataFrame:
    new_df = pd.DataFrame(rows)
    if manifest_path.exists():
        existing_df = pd.read_csv(manifest_path)
        merged_df = pd.concat([existing_df, new_df], ignore_index=True)
        if "pr_url" in merged_df.columns:
            merged_df = merged_df.drop_duplicates(subset=["pr_url"], keep="last")
    else:
        merged_df = new_df
    merged_df.to_csv(manifest_path, index=False)
    return merged_df


def _github_mcp_preflight(settings: Any) -> dict[str, Any]:
    if not getattr(settings, "github_mcp_enabled", False):
        return {"status": "disabled", "required_tools": ["get_commits_for_path"]}

    env = None
    if getattr(settings, "github_personal_access_token", ""):
        env = dict(os.environ)
        env["GITHUB_PERSONAL_ACCESS_TOKEN"] = settings.github_personal_access_token

    client = MCPClient(
        command=settings.github_mcp_command,
        args=settings.github_mcp_args,
        cwd=settings.github_mcp_cwd,
        env=env,
        timeout_seconds=settings.github_mcp_timeout_seconds,
    )
    try:
        tools = sorted(client.list_tools())
    except Exception as exc:  # noqa: BLE001 - benchmark runs should record MCP degradation, not fail
        return {
            "status": "error",
            "required_tools": ["get_commits_for_path"],
            "available_tools": [],
            "missing_required_tools": ["get_commits_for_path"],
            "error": f"{exc.__class__.__name__}: {exc}",
        }

    missing = [name for name in ("get_commits_for_path",) if name not in tools]
    return {
        "status": "ok" if not missing else "degraded",
        "required_tools": ["get_commits_for_path"],
        "available_tools": tools,
        "missing_required_tools": missing,
    }


def _normalize_repo_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


def _path_from_mapping_or_model(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, dict):
        return _normalize_repo_path(str(item.get("file_path") or item.get("path") or ""))
    return _normalize_repo_path(str(getattr(item, "file_path", "") or getattr(item, "path", "") or ""))


def _candidate_from_result(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("candidate")
    return getattr(item, "candidate", None)


def _check_id_from_result(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("check_id") or "")
    return str(getattr(item, "check_id", "") or "")


def _load_positive_samples_by_pr(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        pr_url = str(row.get("githubPrUrl") or row.get("pr_url") or "").strip()
        if not pr_url:
            continue
        comments = row.get("comments") if isinstance(row.get("comments"), list) else []
        labels = []
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            label_path = _normalize_repo_path(str(comment.get("path") or ""))
            if not label_path:
                continue
            labels.append(
                {
                    "path": label_path,
                    "from_line": comment.get("from_line"),
                    "to_line": comment.get("to_line"),
                    "category": comment.get("category"),
                    "is_ai_comment": comment.get("is_ai_comment"),
                }
            )
        if labels:
            out[pr_url] = labels
    return out


def _paths_from_raw_stage(raw: dict[str, Any], final_findings: Iterable[Any]) -> dict[str, set[str]]:
    metadata = raw.get("metadata", {}) if isinstance(raw.get("metadata"), dict) else {}
    review_meta = metadata.get("review_checks", {}) if isinstance(metadata.get("review_checks"), dict) else {}
    by_task = review_meta.get("by_task", {}) if isinstance(review_meta.get("by_task"), dict) else {}

    compiled_paths: set[str] = set()
    for task_meta in by_task.values():
        if not isinstance(task_meta, dict):
            continue
        for check in task_meta.get("compiled_checks") or []:
            path = _path_from_mapping_or_model(check)
            if path:
                compiled_paths.add(path)
    for check in raw.get("review_checks", []) or []:
        path = _path_from_mapping_or_model(check)
        if path:
            compiled_paths.add(path)
    for invalid in raw.get("invalid_review_checks", []) or []:
        check = invalid.get("check") if isinstance(invalid, dict) else getattr(invalid, "check", None)
        path = _path_from_mapping_or_model(check)
        if path:
            compiled_paths.add(path)

    valid_paths = {_path_from_mapping_or_model(check) for check in raw.get("review_checks", []) or []}
    valid_paths.discard("")
    checks_by_id: dict[str, str] = {}
    for check in raw.get("review_checks", []) or []:
        check_id = str(check.get("check_id") if isinstance(check, dict) else getattr(check, "check_id", "") or "")
        path = _path_from_mapping_or_model(check)
        if check_id and path:
            checks_by_id[check_id] = path

    request_paths: set[str] = set()
    for req in raw.get("focused_context_requests", []) or []:
        paths = req.get("file_paths", []) if isinstance(req, dict) else getattr(req, "file_paths", [])
        if isinstance(paths, list):
            request_paths.update(_normalize_repo_path(str(path)) for path in paths if str(path).strip())

    result_paths: set[str] = set()
    focused_results = raw.get("focused_context_results", {}) or {}
    if isinstance(focused_results, dict):
        for result in focused_results.values():
            if not isinstance(result, dict):
                continue
            for key in ("file_snippets", "file_contents_full"):
                values = result.get(key, {})
                if isinstance(values, dict):
                    result_paths.update(_normalize_repo_path(str(path)) for path in values.keys())
            hits = result.get("search_hits", {})
            if isinstance(hits, dict):
                for hit_list in hits.values():
                    if not isinstance(hit_list, list):
                        continue
                    for hit in hit_list:
                        path = _path_from_mapping_or_model(hit)
                        if path:
                            result_paths.add(path)

    executor_paths: set[str] = set()
    candidate_paths: set[str] = set()
    for result in raw.get("review_check_results", []) or []:
        check_path = checks_by_id.get(_check_id_from_result(result), "")
        if check_path:
            executor_paths.add(check_path)
        candidate = _candidate_from_result(result)
        path = _path_from_mapping_or_model(candidate)
        if path:
            candidate_paths.add(path)
    for candidate in raw.get("candidate_findings", []) or []:
        path = _path_from_mapping_or_model(candidate)
        if path:
            candidate_paths.add(path)

    final_paths = {_path_from_mapping_or_model(finding) for finding in final_findings}
    final_paths.discard("")
    return {
        "compiled": compiled_paths,
        "valid": valid_paths,
        "focused_requested": request_paths,
        "focused_result": result_paths,
        "executed": executor_paths,
        "candidate": candidate_paths,
        "final": final_paths,
    }


def _coverage_audit_for_pr(
    *,
    pr_url: str,
    slug: str,
    raw: dict[str, Any],
    final_findings: Iterable[Any],
    labels: list[dict[str, Any]],
) -> dict[str, Any]:
    stage_paths = _paths_from_raw_stage(raw, final_findings)
    path_counts = Counter(label["path"] for label in labels)
    records = []
    for path, count in sorted(path_counts.items()):
        records.append(
            {
                "path": path,
                "positive_label_count": count,
                "compiled": path in stage_paths["compiled"],
                "valid": path in stage_paths["valid"],
                "focused_requested": path in stage_paths["focused_requested"],
                "focused_result": path in stage_paths["focused_result"],
                "executed": path in stage_paths["executed"],
                "candidate": path in stage_paths["candidate"],
                "final": path in stage_paths["final"],
            }
        )
    summary = {
        "positive_label_count": len(labels),
        "positive_path_count": len(path_counts),
        "compiled_path_count": sum(1 for item in records if item["compiled"]),
        "valid_path_count": sum(1 for item in records if item["valid"]),
        "focused_requested_path_count": sum(1 for item in records if item["focused_requested"]),
        "focused_result_path_count": sum(1 for item in records if item["focused_result"]),
        "executed_path_count": sum(1 for item in records if item["executed"]),
        "candidate_path_count": sum(1 for item in records if item["candidate"]),
        "final_path_count": sum(1 for item in records if item["final"]),
    }
    return {"pr_url": pr_url, "slug": slug, "summary": summary, "paths": records}


def _write_coverage_audit(path: Path, records: List[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "prs_with_positive_labels": sum(1 for item in records if item["summary"]["positive_label_count"] > 0),
        "positive_label_count": sum(item["summary"]["positive_label_count"] for item in records),
        "positive_path_count": sum(item["summary"]["positive_path_count"] for item in records),
        "compiled_path_count": sum(item["summary"]["compiled_path_count"] for item in records),
        "valid_path_count": sum(item["summary"]["valid_path_count"] for item in records),
        "candidate_path_count": sum(item["summary"]["candidate_path_count"] for item in records),
        "final_path_count": sum(item["summary"]["final_path_count"] for item in records),
    }
    payload = {"schema_version": "1", "summary": totals, "prs": records}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def _review_check_metrics(result: dict[str, Any]) -> dict[str, Any]:
    def _decision(item: Any) -> str:
        return str(getattr(item, "decision", None) or (item.get("decision") if isinstance(item, dict) else ""))

    checks = result.get("review_checks", []) or []
    invalid = result.get("invalid_review_checks", []) or []
    check_results = result.get("review_check_results", []) or []
    candidates = result.get("candidate_findings", []) or []
    latest_by_check: dict[str, Any] = {}
    latest_by_candidate: dict[str, Any] = {}
    for item in check_results:
        check_id = getattr(item, "check_id", None) or (item.get("check_id") if isinstance(item, dict) else "")
        if check_id:
            latest_by_check[str(check_id)] = item
        candidate = getattr(item, "candidate", None) or (item.get("candidate") if isinstance(item, dict) else None)
        if candidate is None:
            continue
        candidate_id = (
            getattr(candidate, "candidate_id", None)
            or (candidate.get("candidate_id") if isinstance(candidate, dict) else "")
        )
        if candidate_id:
            latest_by_candidate[str(candidate_id)] = item
    gate_drop_count = 0
    gate_pass_count = 0
    invalid_reason_counts: Counter[str] = Counter()
    health_warnings: List[str] = []
    metadata = result.get("metadata", {}) or {}
    block = metadata.get("review_checks", {}) if isinstance(metadata, dict) else {}
    by_task = block.get("by_task", {}) if isinstance(block, dict) else {}
    if isinstance(by_task, dict):
        for task_meta in by_task.values():
            if not isinstance(task_meta, dict):
                continue
            gate = task_meta.get("gate", {})
            if isinstance(gate, dict):
                gate_drop_count += int(gate.get("dropped_count") or 0)
                gate_pass_count += int(gate.get("promoted_count") or 0)
                health_warnings.extend(str(item) for item in gate.get("health_warnings", []) or [])
            validation = task_meta.get("validation", {})
            if isinstance(validation, dict):
                invalid_reason_counts.update(
                    {
                        str(reason): int(count)
                        for reason, count in (validation.get("reason_counts", {}) or {}).items()
                    }
                )
            health_warnings.extend(str(item) for item in task_meta.get("health_warnings", []) or [])
    latest_results = list(latest_by_check.values())
    invalid_reason_total = sum(invalid_reason_counts.values())
    dominant_invalid_reason = ""
    if invalid_reason_counts:
        reason, count = invalid_reason_counts.most_common(1)[0]
        dominant_invalid_reason = reason
        if invalid_reason_total and count / invalid_reason_total >= 0.5:
            health_warnings.append(f"dominant_invalid_reason:{reason}")
    if checks and latest_results and not latest_by_candidate:
        health_warnings.append("no_executor_candidates_for_valid_checks")
    return {
        "compiled_check_count": len(checks) + len(invalid),
        "valid_check_count": len(checks),
        "invalid_check_count": len(invalid),
        "check_candidate_count": len(latest_by_candidate),
        "no_finding_check_count": sum(
            1 for item in latest_results if _decision(item) == "no_finding"
        ),
        "unsupported_check_count": sum(
            1 for item in latest_results if _decision(item) == "unsupported"
        ),
        "suppressed_check_count": sum(
            1 for item in latest_results if _decision(item) == "suppressed"
        ),
        "budget_exhausted_check_count": sum(
            1 for item in latest_results if _decision(item) == "budget_exhausted"
        ),
        "evidence_gate_pass_count": gate_pass_count,
        "evidence_gate_drop_count": gate_drop_count,
        "candidate_count": len(candidates),
        "invalid_reason_counts": json.dumps(dict(sorted(invalid_reason_counts.items())), sort_keys=True),
        "dominant_invalid_reason": dominant_invalid_reason,
        "review_check_health_warnings": json.dumps(sorted(set(health_warnings))),
    }


def _load_snapshot_for_resume(snapshot_id: str, logger: logging.Logger) -> Dict[str, Any]:
    """Load snapshot data for resuming review with new diff."""
    settings = get_settings()
    loader = SnapshotLoader(settings)
    
    logger.info("Loading snapshot for resume: %s", snapshot_id)
    
    try:
        snapshot = loader.load_snapshot_pointer(snapshot_id)
    except FileNotFoundError:
        logger.error("Snapshot not found: %s", snapshot_id)
        sys.exit(1)
    except ValueError as e:
        logger.error("Invalid snapshot format: %s - %s", snapshot_id, e)
        sys.exit(1)
    
    # Load all components
    graph_payload = loader.load_graph_payload(snapshot.snapshot_root)
    topology = loader.load_topology(snapshot.snapshot_root)
    community_summaries = loader.load_community_shards(snapshot.snapshot_root)
    global_summary = loader.load_global_summary(snapshot.snapshot_root)
    
    logger.info(
        "Loaded snapshot: %s (status=%s, communities=%s, nodes=%s, edges=%s)",
        snapshot_id, snapshot.status, snapshot.community_count,
        snapshot.total_nodes, snapshot.total_edges
    )
    
    # Get repo_path from metadata (written by snapshot_writer)
    repo_path = snapshot.metadata.get("repo_path", "")
    
    return {
        "snapshot_root": snapshot.snapshot_root,
        "snapshot_id": snapshot.snapshot_id,
        "repo_path": repo_path,
        "graph_payload": graph_payload,
        "topology": topology,
        "community_summaries": community_summaries,
        "global_summary": global_summary,
    }


def _load_pr_urls(
    source: pd.DataFrame | Path,
    limit: Optional[int],
    logger: logging.Logger,
    pr_url: Optional[str] = None,
    pr_urls: Optional[List[str]] = None,
    dataset_range: Optional[DatasetRange] = None,
) -> List[str]:
    if dataset_range is not None:
        dataset_range.validate()

    if pr_url and pr_urls:
        raise ValueError("Specify only one of pr_url or pr_urls")

    if isinstance(source, pd.DataFrame):
        df = source
    else:
        logger.info("Reading AACR-Bench processed dataset from %s", source)
        df = pd.read_csv(source)

    if "pr_url" not in df.columns:
        raise ValueError("AACR-Bench dataframe is missing required 'pr_url' column")
    if "target_language" not in df.columns:
        raise ValueError("AACR-Bench dataframe is missing required 'target_language' column")

    working_df = df.copy()
    working_df["pr_url"] = working_df["pr_url"].fillna("").astype(str).map(str.strip)
    working_df["target_language"] = working_df["target_language"].fillna("").astype(str).map(str.strip).str.lower()

    if pr_urls:
        requested_urls = [url.strip() for url in pr_urls if url and url.strip()]
        if not requested_urls:
            raise ValueError("pr_urls must contain at least one non-empty PR URL")

        unique_requested_urls = list(dict.fromkeys(requested_urls))
        selected_urls: List[str] = []
        missing_urls: List[str] = []
        non_python_urls: List[str] = []

        for requested_url in unique_requested_urls:
            matches = working_df.loc[working_df["pr_url"] == requested_url]
            if matches.empty:
                missing_urls.append(requested_url)
                continue
            if not (matches["target_language"] == "python").all():
                non_python_urls.append(requested_url)
                continue
            selected_urls.append(requested_url)

        if missing_urls:
            raise ValueError(
                "Requested PR URLs not found in AACR-Bench dataset: " + ", ".join(missing_urls)
            )
        if non_python_urls:
            raise ValueError(
                "Requested PR URLs are not python PRs in AACR-Bench dataset: "
                + ", ".join(non_python_urls)
            )
        return selected_urls

    url_series = working_df["pr_url"].loc[lambda s: s != ""]
    if pr_url:
        requested_url = pr_url.strip()
        url_series = url_series.loc[lambda s: s == requested_url]
        if url_series.empty:
            raise ValueError(f"Requested PR URL not found in AACR-Bench dataset: {requested_url}")

    urls = url_series.drop_duplicates().tolist()
    if dataset_range is not None:
        start_idx = dataset_range.start - 1
        end_idx = dataset_range.end
        urls = urls[start_idx:end_idx]
    if limit is not None and limit > 0:
        urls = urls[:limit]
    return urls


def _git_cli_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            check=True,
            timeout=15,
        )
        return True
    except Exception:
        return False


def _ensure_snapshot_pr_worktree(
    *,
    snapshot_root: str,
    canonical_repo_url: str,
    pr_number: int,
    logger: logging.Logger,
) -> str:
    """Fetch PR head into ``<snapshot_root>/_reviewer_worktree`` (host git).

    Matches full-graph runs that bind-mount a real directory: review sandbox, verifier,
    and AST see files on disk instead of a bare GitHub URL.
    """
    root = Path(snapshot_root).resolve()
    worktree = root / "_reviewer_worktree"
    marker = worktree / ".bw_snapshot_pr_worktree"
    marker_payload = f"{canonical_repo_url}\npull/{pr_number}/head\n"

    if worktree.is_dir() and marker.is_file():
        try:
            if marker.read_text(encoding="utf-8") == marker_payload:
                logger.info("Snapshot resume: reusing PR worktree %s", worktree)
                return str(worktree.resolve())
        except OSError:
            pass

    if worktree.exists():
        shutil.rmtree(worktree)

    worktree.mkdir(parents=True)
    logger.info(
        "Snapshot resume: fetching pull/%s/head into %s (host git)",
        pr_number,
        worktree,
    )
    subprocess.run(
        ["git", "init"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", canonical_repo_url],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    fetch = subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", f"pull/{pr_number}/head"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=1200,
    )
    if fetch.returncode != 0:
        raise RuntimeError(fetch.stderr or fetch.stdout or "git fetch failed")
    checkout = subprocess.run(
        ["git", "checkout", "FETCH_HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if checkout.returncode != 0:
        raise RuntimeError(checkout.stderr or checkout.stdout or "git checkout failed")
    marker.write_text(marker_payload, encoding="utf-8")
    return str(worktree.resolve())


def _invoke_for_pr(
    run_id: str,
    pr_url: str,
    context: PullRequestContext,
    repo_root: Optional[Path],
    trace: bool,
    started_at: str,
    run_reviewer_fn: Callable[[GraphState], dict[str, Any]],
    experiment_tag: str,
    logger: logging.Logger,
    snapshot_data: Optional[Dict[str, Any]] = None,
    mcp_preflight: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    # Determine repo_path and graph_run_id
    graph_run_id = _graph_thread_id(run_id, pr_url, snapshot_data)
    repo_url = f"https://github.com/{context.repo}"
    snapshot_repo_extra: Dict[str, Any] = {}

    if snapshot_data:
        if repo_root is not None:
            repo_path = str(repo_root.resolve())
        else:
            meta_rp = (snapshot_data.get("repo_path") or "").strip()
            if meta_rp and Path(meta_rp).is_dir():
                repo_path = str(Path(meta_rp).resolve())
            elif meta_rp.startswith("http://") or meta_rp.startswith("https://"):
                settings = get_settings()
                if settings.reviewer_allow_host_pr_worktree and _git_cli_available():
                    try:
                        repo_path = _ensure_snapshot_pr_worktree(
                            snapshot_root=snapshot_data["snapshot_root"],
                            canonical_repo_url=repo_url,
                            pr_number=context.number,
                            logger=logger,
                        )
                        snapshot_repo_extra["snapshot_pr_worktree_auto_cloned"] = True
                        logger.warning(
                            "Snapshot resume: using host PR worktree (reviewer_allow_host_pr_worktree=true). "
                            "PR content is on disk under the snapshot root."
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Snapshot resume: could not prepare local PR worktree (%s: %s); "
                            "using URL repo_path (sandbox clones inside Docker).",
                            exc.__class__.__name__,
                            exc,
                        )
                        repo_path = repo_url
                else:
                    if not settings.reviewer_allow_host_pr_worktree:
                        logger.info(
                            "Snapshot resume: repo_path=%s (sandbox-only; set "
                            "REVIEW_REVIEWER_ALLOW_HOST_PR_WORKTREE=true to allow host worktree).",
                            repo_url,
                        )
                    else:
                        logger.warning(
                            "Snapshot resume: host git not found; using URL repo_path (sandbox clone)."
                        )
                    repo_path = repo_url
            else:
                repo_path = meta_rp or repo_url

        if trace:
            trace_logger = logging.getLogger("research_pipeline.reviewer_trace")
            trace_logger.info(
                "TRACE snapshot_load run_id=%s snapshot=%s snapshot_meta_repo=%s resolved_repo_path=%s",
                graph_run_id,
                snapshot_data["snapshot_id"][:8],
                snapshot_data["repo_path"],
                repo_path,
            )
    else:
        repo_path = str(repo_root.resolve()) if repo_root is not None else repo_url
    
    # Base state
    initial_state: GraphState = {
        "run_id": graph_run_id,
        "repo_path": repo_path,
        "git_diff": context.unified_diff,
        "user_goals": "",
        "global_insights": [],
        "findings": [],
        "reviewer_worker_reports": [],
        "candidate_findings": [],
        "reflection_reports": [],
        "focused_context_requests": [],
        "focused_context_results": {},
        "llm_trace": [],
        "token_usage": 0,
        "node_history": [],
        "metadata": {
            "experiment": experiment_tag,
            "run_id": run_id,
            "graph_run_id": graph_run_id,
            "pr_started_at": started_at,
            "pr_url": pr_url,
            "pr_title": context.title,
            "pr_description": context.body,
            "pr_repo": context.repo,
            "pr_number": context.number,
            "review_repo_url": repo_url,
            "review_pr_number": context.number,
            "review_trace_enabled": trace,
            "mcp_preflight": dict(mcp_preflight or {}),
        },
    }
    
    # Inject snapshot data if provided
    if snapshot_data:
        topology = snapshot_data["topology"]
        community_count = int(getattr(topology, "community_count", 0) or 0)
        if not community_count and isinstance(topology, dict):
            community_count = int(topology.get("community_count") or len(topology.get("communities") or []))
        if not community_count:
            community_count = len(snapshot_data.get("community_summaries") or [])
        exploration_context_ready = {
            "source": "loaded",
            "snapshot_id": snapshot_data["snapshot_id"],
            "has_graph": isinstance(snapshot_data.get("graph_payload"), dict),
            "has_topology": bool(topology),
            "community_count": community_count,
            "has_global_summary": bool(str(snapshot_data.get("global_summary") or "").strip()),
        }
        preflight_summary = PreflightSummary(
            manifest_id=f"snapshot_{snapshot_data['snapshot_id']}",
            total_files_changed=0,
            total_hunks=0,
            total_additions=0,
            total_deletions=0,
            has_errors=False,
            has_ambiguity=False,
        )
        
        initial_state.update({
            "structural_graph_node_link": snapshot_data["graph_payload"],
            "structural_topology": snapshot_data["topology"],
            "community_summaries": [s.model_dump() for s in snapshot_data["community_summaries"]],
            "global_summary": snapshot_data["global_summary"],
            "snapshot_root": snapshot_data["snapshot_root"],
            "snapshot_id": snapshot_data["snapshot_id"],
            "snapshot_source": "loaded",
            "preflight_summary": preflight_summary,
            "preflight_errors": [],
            "preflight_warnings": [],
            "unverified_call_targets": [],
            "knowledge_gaps": [],
            "metadata": {
                **initial_state["metadata"],
                **snapshot_repo_extra,
                "snapshot_loaded": True,
                "snapshot_source": "loaded",
                "exploration_context_ready": exploration_context_ready,
                "docs_prebrief": {
                    "status": "skipped_snapshot_resume",
                    "reason": "snapshot_resume_uses_precomputed_context",
                },
            },
        })
    
    return run_reviewer_fn(initial_state)


def _cleanup_pr_checkpoints(settings: Any, graph_run_id: str, logger: logging.Logger) -> bool:
    if not settings.redis_enabled or not settings.reviewer_cleanup_redis_checkpoints:
        return False

    try:
        delete_checkpoint_thread(settings, graph_run_id)
    except Exception as exc:  # noqa: BLE001 - cleanup must not fail experiments
        logger.warning(
            "Redis checkpoint cleanup failed for thread_id=%s: %s: %s",
            graph_run_id,
            exc.__class__.__name__,
            exc,
        )
        return False

    logger.info("Deleted Redis checkpoints for thread_id=%s", graph_run_id)
    return True


def run_aacr_reviewer(
    dataset_path: Path = DEFAULT_AACR_PROCESSED_PATH,
    run_id: Optional[str] = None,
    limit: Optional[int] = None,
    pr_url: Optional[str] = None,
    pr_urls: Optional[List[str]] = None,
    dataset_range: Optional[DatasetRange] = None,
    output_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    trace: bool = False,
    use_basic_graph: bool = False,
    cli_flags: Optional[dict[str, Any]] = None,
    snapshot_id: Optional[str] = None,
) -> ReviewerRunArtifacts:
    settings = get_settings()
    ensure_directories([LOG_DIR])
    logger = configure_logger(LOG_DIR / "reviewer_agent_aacr.log")

    resolved_run_id = run_id or uuid.uuid4().hex[:12]
    resolved_output_root = output_root or settings.reviewer_agent_output_dir
    run_dir, raw_dir, findings_dir = _prepare_output_dirs(Path(resolved_output_root), resolved_run_id)
    run_started_at = _utc_now_iso()

    experiment_tag = BASIC_EXPERIMENT_TAG if use_basic_graph else EXPERIMENT_TAG
    run_reviewer_fn = run_reviewer_basic if use_basic_graph else run_reviewer
    mcp_preflight = _github_mcp_preflight(settings)
    logger.info("GitHub MCP preflight status=%s", mcp_preflight.get("status"))

    # Load snapshot if provided (once before the loop)
    snapshot_data = None
    if snapshot_id:
        snapshot_data = _load_snapshot_for_resume(snapshot_id, logger)
        logger.info("Snapshot repo for validation: %s", snapshot_data["repo_path"])

    logger.info(
        "Starting reviewer-graph AACR run run_id=%s dataset=%s output=%s trace=%s basic=%s",
        resolved_run_id,
        dataset_path,
        run_dir,
        trace,
        use_basic_graph,
    )

    selected_pr_urls = _load_pr_urls(
        dataset_path,
        limit=limit,
        logger=logger,
        pr_url=pr_url,
        pr_urls=pr_urls,
        dataset_range=dataset_range,
    )
    # Preserve CLI filter before the loop shadows `pr_url` with each row's URL.
    pr_url_filter_for_meta = pr_url.strip() if pr_url else ""
    logger.info("Reviewer-graph AACR run will process %s unique PR URLs", len(selected_pr_urls))
    positive_samples_by_pr = _load_positive_samples_by_pr(DEFAULT_POSITIVE_SAMPLES_PATH)

    enricher = GitHubPullRequestEnricher(
        logger=logger,
        token=settings.github_personal_access_token,
    )

    manifest_rows: List[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    run_started = time.perf_counter()
    total_llm_tokens = 0
    coverage_records: List[dict[str, Any]] = []

    for idx, pr_url in enumerate(selected_pr_urls, start=1):
        positive_labels = positive_samples_by_pr.get(pr_url, [])
        slug = _slug_for_pr_url(pr_url)
        pr_started_at = _utc_now_iso()
        row: dict[str, Any] = {
            "pr_url": pr_url,
            "slug": slug,
            "started_at": pr_started_at,
            "finished_at": "",
            "status": "pending",
            "raw_path": "",
            "findings_path": "",
            "finding_count": 0,
            "elapsed_ms": 0,
            "error": "",
            "redis_checkpoints_cleaned": False,
            "token_usage": 0,
            "compiled_check_count": 0,
            "valid_check_count": 0,
            "invalid_check_count": 0,
            "check_candidate_count": 0,
            "no_finding_check_count": 0,
            "unsupported_check_count": 0,
            "suppressed_check_count": 0,
            "budget_exhausted_check_count": 0,
            "evidence_gate_pass_count": 0,
            "evidence_gate_drop_count": 0,
            "candidate_count": 0,
            "final_finding_count": 0,
            "invalid_reason_counts": "{}",
            "dominant_invalid_reason": "",
            "review_check_health_warnings": "[]",
            "positive_label_count": len(positive_labels),
            "positive_path_count": len({label["path"] for label in positive_labels}),
            "positive_compiled_path_count": 0,
            "positive_valid_path_count": 0,
            "positive_candidate_path_count": 0,
            "positive_final_path_count": 0,
        }

        context = enricher.fetch_pr_context(pr_url)
        if context is None:
            row["status"] = "skipped_enrichment_failed"
            row["finished_at"] = _utc_now_iso()
            row["error"] = "github_pr_context_unavailable"
            coverage_records.append(
                _coverage_audit_for_pr(
                    pr_url=pr_url,
                    slug=slug,
                    raw={},
                    final_findings=[],
                    labels=positive_labels,
                )
            )
            manifest_rows.append(row)
            failed += 1
            logger.warning("[%s/%s] Skipping %s: enrichment failed", idx, len(selected_pr_urls), pr_url)
            continue

        # Validate repo match if using snapshot
        if snapshot_data:
            expected_repo_url = f"https://github.com/{context.repo}"
            if snapshot_data["repo_path"] != expected_repo_url:
                row["status"] = "error"
                row["finished_at"] = _utc_now_iso()
                row["error"] = f"snapshot_repo_mismatch: expected {expected_repo_url}, got {snapshot_data['repo_path']}"
                coverage_records.append(
                    _coverage_audit_for_pr(
                        pr_url=pr_url,
                        slug=slug,
                        raw={},
                        final_findings=[],
                        labels=positive_labels,
                    )
                )
                manifest_rows.append(row)
                failed += 1
                logger.error(
                    "[%s/%s] Snapshot repo mismatch for %s: expected %s, got %s",
                    idx, len(selected_pr_urls), pr_url, expected_repo_url, snapshot_data["repo_path"]
                )
                continue

        graph_run_id = _graph_thread_id(resolved_run_id, pr_url, snapshot_data)
        started = time.perf_counter()
        try:
            result = _invoke_for_pr(
                run_id=resolved_run_id,
                pr_url=pr_url,
                context=context,
                repo_root=repo_root,
                trace=trace,
                started_at=pr_started_at,
                run_reviewer_fn=run_reviewer_fn,
                experiment_tag=experiment_tag,
                logger=logger,
                snapshot_data=snapshot_data,
                mcp_preflight=mcp_preflight,
            )
        except Exception as exc:  # noqa: BLE001 - per-PR isolation; harness continues
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            row["status"] = "error"
            row["finished_at"] = _utc_now_iso()
            row["elapsed_ms"] = elapsed_ms
            row["error"] = f"{exc.__class__.__name__}: {exc}"
            row["redis_checkpoints_cleaned"] = _cleanup_pr_checkpoints(
                settings,
                graph_run_id,
                logger,
            )
            coverage_records.append(
                _coverage_audit_for_pr(
                    pr_url=pr_url,
                    slug=slug,
                    raw={},
                    final_findings=[],
                    labels=positive_labels,
                )
            )
            manifest_rows.append(row)
            failed += 1
            logger.exception("[%s/%s] Reviewer-graph run failed for %s", idx, len(selected_pr_urls), pr_url)
            continue

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        pr_finished_at = _utc_now_iso()
        pr_tokens = int(result.get("token_usage") or 0)
        total_llm_tokens += pr_tokens
        metadata = dict(result.get("metadata", {}))
        metadata["pr_finished_at"] = pr_finished_at
        metadata["llm_total_tokens"] = pr_tokens
        result["metadata"] = metadata
        findings = result.get("final_findings") or result.get("findings", []) or []
        raw_path = _write_raw(raw_dir, slug, result)
        findings_path = _write_findings(findings_dir, slug, findings)
        coverage_record = _coverage_audit_for_pr(
            pr_url=pr_url,
            slug=slug,
            raw={
                "metadata": result.get("metadata", {}) or {},
                "review_checks": result.get("review_checks", []) or [],
                "invalid_review_checks": result.get("invalid_review_checks", []) or [],
                "review_check_results": result.get("review_check_results", []) or [],
                "focused_context_requests": result.get("focused_context_requests", []) or [],
                "focused_context_results": result.get("focused_context_results", {}) or {},
                "candidate_findings": result.get("candidate_findings", []) or [],
            },
            final_findings=findings,
            labels=positive_labels,
        )
        coverage_records.append(coverage_record)

        row["status"] = "ok"
        row["finished_at"] = pr_finished_at
        row["raw_path"] = str(raw_path.relative_to(run_dir))
        row["findings_path"] = str(findings_path.relative_to(run_dir))
        row["finding_count"] = len(findings)
        check_metrics = _review_check_metrics(result)
        row.update(check_metrics)
        row["positive_compiled_path_count"] = coverage_record["summary"]["compiled_path_count"]
        row["positive_valid_path_count"] = coverage_record["summary"]["valid_path_count"]
        row["positive_candidate_path_count"] = coverage_record["summary"]["candidate_path_count"]
        row["positive_final_path_count"] = coverage_record["summary"]["final_path_count"]
        if positive_labels and int(row.get("check_candidate_count") or 0) == 0:
            warnings = set(json.loads(str(row.get("review_check_health_warnings") or "[]")))
            warnings.add("known_positive_no_draft_candidate")
            row["review_check_health_warnings"] = json.dumps(sorted(warnings))
        row["final_finding_count"] = len(findings)
        row["elapsed_ms"] = elapsed_ms
        row["token_usage"] = pr_tokens
        row["redis_checkpoints_cleaned"] = _cleanup_pr_checkpoints(
            settings,
            graph_run_id,
            logger,
        )
        manifest_rows.append(row)
        succeeded += 1

        logger.info(
            "[%s/%s] %s ok findings=%s token_usage=%s elapsed_ms=%s",
            idx,
            len(selected_pr_urls),
            slug,
            len(findings),
            pr_tokens,
            elapsed_ms,
        )

    manifest_path = run_dir / "manifest.csv"
    manifest_df = _write_manifest(manifest_path, manifest_rows)
    coverage_audit_path = run_dir / "coverage_audit.json"
    coverage_audit = _write_coverage_audit(coverage_audit_path, coverage_records)

    run_meta_path = run_dir / "run_meta.json"
    run_finished_at = _utc_now_iso()
    run_meta = {
        "experiment": experiment_tag,
        "run_id": resolved_run_id,
        "started_at": run_started_at,
        "finished_at": run_finished_at,
        "dataset": AACR_BENCH_CONFIG.key,
        "dataset_path": str(dataset_path),
        "planner_model_key": settings.reviewer_planner_model_key,
        "worker_model_key": settings.reviewer_worker_model_key,
        "reviewer_use_legacy_specialist_workers": settings.reviewer_use_legacy_specialist_workers,
        "reviewer_check_mode": settings.reviewer_check_mode,
        "pr_url_filter": pr_url_filter_for_meta,
        "dataset_range": (
            {"start": dataset_range.start, "end": dataset_range.end}
            if dataset_range is not None
            else None
        ),
        "repo_root": str(repo_root) if repo_root is not None else "",
        "trace": trace,
        "basic_graph": use_basic_graph,
        "cli_flags": dict(cli_flags) if cli_flags else {},
        "mcp_preflight": mcp_preflight,
        "coverage_audit_path": str(coverage_audit_path.relative_to(run_dir)),
        "coverage_audit_summary": coverage_audit["summary"],
        "redis_checkpoint_cleanup_enabled": (
            settings.redis_enabled and settings.reviewer_cleanup_redis_checkpoints
        ),
        "total_prs": len(selected_pr_urls),
        "manifest_total_rows": len(manifest_df),
        "succeeded": succeeded,
        "failed": failed,
        "total_llm_tokens": total_llm_tokens,
        "elapsed_ms": int((time.perf_counter() - run_started) * 1000),
    }
    run_meta_path.write_text(json.dumps(run_meta, indent=2), encoding="utf-8")

    logger.info(
        "Finished reviewer-graph AACR run run_id=%s succeeded=%s failed=%s total_llm_tokens=%s elapsed_ms=%s",
        resolved_run_id,
        succeeded,
        failed,
        total_llm_tokens,
        run_meta["elapsed_ms"],
    )

    return ReviewerRunArtifacts(
        run_id=resolved_run_id,
        output_dir=run_dir,
        manifest_path=manifest_path,
        raw_dir=raw_dir,
        findings_dir=findings_dir,
        run_meta_path=run_meta_path,
        processed=len(selected_pr_urls),
        succeeded=succeeded,
        failed=failed,
    )
