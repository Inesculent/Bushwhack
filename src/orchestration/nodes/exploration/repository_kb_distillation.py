"""LLM-backed, bounded distillation over Repository KB records."""

from __future__ import annotations

import logging
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from src.config import Settings, get_settings
from src.domain.schemas import (
    RepositoryKBCommunityDistillationOutput,
    RepositoryKBRepoDistillationOutput,
    RepositoryKBShardDistillationOutput,
    ReviewKBRecord,
)
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.local_status import (
    is_local_model,
    is_timeout_exception,
    local_llm_server_active,
    sleep_for_retry,
)
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_from_exception, trace_llm_call
from src.infrastructure.review_kb import ReviewKBBundle, rebuild_review_kb_lexical_index
from src.infrastructure.review_kb_distillation import (
    RepositoryKBDistillationPlan,
    RepositoryKBDistillationPlanner,
    community_merge_pack,
    build_repo_distillation_pack,
    community_summary_record_from_distillation,
    fallback_shard_summary_record,
    mark_distillation_failed,
    pack_to_prompt_json,
    replace_summary_records,
    repo_summary_record_from_distillation,
    shard_summary_record_from_distillation,
    shard_to_prompt_pack,
)
from src.orchestration.prompts.exploration_prompts import (
    render_repository_kb_community_merge_prompt,
    render_repository_kb_community_distill_prompt,
    render_repository_kb_repo_distill_prompt,
    render_repository_kb_shard_distill_prompt,
)

logger = logging.getLogger(__name__)

_SHARD_COMPACT_RETRY_APPENDIX = (
    "\n\n## OUTPUT BUDGET (retry - required)\n"
    "Your previous shard response exceeded the length limit. Return the smallest valid JSON only. "
    "For each shard: summary <= 60 words, at most 2 responsibilities, 2 public_contracts, "
    "2 important_facts, 2 data_shape_notes, 2 risk_surfaces, 1 uncertainty, 2 retrieval_hints, "
    "and at most 8 source_record_ids. Do not list every record."
)

_GLOBAL_COMPACT_RETRY_APPENDIX = (
    "\n\n## OUTPUT BUDGET (retry - required)\n"
    "Return the smallest valid JSON only. Keep the repository map compact: "
    "summary <= 120 words and at most 5 items per list."
)


class _DistillationBudget:
    def __init__(self, settings: Settings) -> None:
        ceiling = settings.repository_kb_distillation_hard_token_ceiling
        self.ceiling = int(ceiling) if settings.repository_kb_distillation_budget_mode == "adaptive" and ceiling else None
        self.used = 0

    def estimate(self, prompt: str, max_completion_tokens: int) -> int:
        return max(1, int(len(prompt) / 4)) + int(max_completion_tokens)

    def can_call(self, prompt: str, max_completion_tokens: int) -> tuple[bool, int]:
        estimate = self.estimate(prompt, max_completion_tokens)
        if self.ceiling is not None and self.used + estimate > self.ceiling:
            return False, estimate
        return True, estimate

    def add(self, actual_tokens: int, estimate: int) -> None:
        self.used += int(actual_tokens or estimate or 0)


def _metadata_int(record: ReviewKBRecord, key: str, default: int = -1) -> int:
    value = record.metadata.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_length_finish_error(exc: Exception) -> bool:
    if "LengthFinish" in exc.__class__.__name__:
        return True
    msg = str(exc).lower()
    return "length limit" in msg or "length_finish" in msg


def _compact_retry_completion_tokens(prompt: str, settings: Settings) -> int:
    """Keep compact retries bounded without starving structured JSON output."""
    prompt_tokens = max(1, int(len(prompt) / 4))
    target = max(1536, int(prompt_tokens * 0.5))
    return min(settings.repository_kb_distillation_max_completion_tokens, target)


def _cache_dir(settings: Settings) -> Path:
    return Path(settings.snapshot_base_path).resolve() / "repository_kb_distillation_cache"


def _cache_key(*, namespace: str, kind: str, model_key: str, pack: Dict[str, Any]) -> str:
    payload = {
        "namespace": namespace,
        "kind": kind,
        "model_key": model_key,
        "pack": pack,
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_cached_record(
    *,
    settings: Settings,
    namespace: str,
    kind: str,
    model_key: str,
    pack: Dict[str, Any],
) -> ReviewKBRecord | None:
    path = _cache_dir(settings) / f"{_cache_key(namespace=namespace, kind=kind, model_key=model_key, pack=pack)}.json"
    if not path.is_file():
        return None
    try:
        record = ReviewKBRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    meta = dict(record.metadata)
    meta["cache_hit"] = True
    meta["llm_distillation_status"] = "cache_hit"
    return record.model_copy(update={"metadata": meta})


def _save_cached_record(
    record: ReviewKBRecord,
    *,
    settings: Settings,
    namespace: str,
    kind: str,
    model_key: str,
    pack: Dict[str, Any],
) -> None:
    try:
        directory = _cache_dir(settings)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_cache_key(namespace=namespace, kind=kind, model_key=model_key, pack=pack)}.json"
        path.write_text(record.model_dump_json(), encoding="utf-8")
    except OSError:
        logger.debug("repository KB distillation cache write failed", exc_info=True)


def _mark_summary_status(record: ReviewKBRecord, status: str, reason: str = "") -> ReviewKBRecord:
    meta = dict(record.metadata)
    meta["llm_distillation_status"] = status
    if reason:
        meta["llm_distillation_reason"] = reason
    return record.model_copy(update={"metadata": meta})


def _mark_distillation_scope(record: ReviewKBRecord, mode: str) -> ReviewKBRecord:
    meta = dict(record.metadata)
    meta["distillation_scope"] = "core_repository" if mode == "full" else mode
    return record.model_copy(update={"metadata": meta})


def _community_ids(bundle: ReviewKBBundle) -> List[int]:
    return [_metadata_int(r, "community_id") for r in bundle.communities]


def _review_neighborhood_community_ids(bundle: ReviewKBBundle, settings: Settings) -> set[int]:
    limit = max(1, int(settings.repository_kb_distillation_review_neighborhood_max_communities))
    files_by_path = {
        str(record.metadata.get("file_path") or ""): _metadata_int(record, "community_id")
        for record in bundle.files
    }
    changed = [str(p) for p in bundle.review_overlay.get("changed_files") or []]
    selected = {files_by_path[p] for p in changed if p in files_by_path}
    selected.discard(-1)

    deps_by_cid = {
        _metadata_int(record, "community_id"): {
            int(dep)
            for dep in (record.metadata.get("cross_community_dependencies") or [])
            if isinstance(dep, int) or str(dep).isdigit()
        }
        for record in bundle.communities
    }
    if selected:
        expanded = set(selected)
        for cid in list(selected):
            expanded.update(deps_by_cid.get(cid, set()))
        for cid, deps in deps_by_cid.items():
            if deps & selected:
                expanded.add(cid)
        selected = expanded
    else:
        ranked = sorted(
            bundle.communities,
            key=lambda r: (
                -len(r.metadata.get("bridge_symbols") or []),
                -len(r.metadata.get("files") or []),
                _metadata_int(r, "community_id"),
            ),
        )
        selected = {_metadata_int(r, "community_id") for r in ranked[: min(4, limit)]}

    ranked_selected = sorted(
        selected,
        key=lambda cid: (
            0 if cid in {files_by_path[p] for p in changed if p in files_by_path} else 1,
            -len(next((r.metadata.get("bridge_symbols") or [] for r in bundle.communities if _metadata_int(r, "community_id") == cid), [])),
            cid,
        ),
    )
    return set(ranked_selected[:limit])


def _selected_community_ids(bundle: ReviewKBBundle, settings: Settings) -> set[int]:
    mode = settings.repository_kb_distillation_mode
    if mode == "full":
        return set(_community_ids(bundle))
    if mode in {"off", "on_demand"}:
        return set()
    return _review_neighborhood_community_ids(bundle, settings)


def _record_distillation_coverage(bundle: ReviewKBBundle) -> Dict[str, int]:
    coverage = {
        "llm_synthesized": 0,
        "inferred": 0,
        "cache_hit": 0,
        "failed": 0,
        "deferred": 0,
        "not_scheduled": 0,
    }
    for record in bundle.summaries:
        if record.metadata.get("summary_scope") not in {"community", "repo"}:
            continue
        status = str(record.metadata.get("llm_distillation_status") or "")
        if status == "cache_hit":
            coverage["cache_hit"] += 1
        elif status == "failed":
            coverage["failed"] += 1
        elif status == "budget_deferred":
            coverage["deferred"] += 1
        elif status == "not_scheduled":
            coverage["not_scheduled"] += 1
        elif record.confidence == "llm_synthesized":
            coverage["llm_synthesized"] += 1
        else:
            coverage["inferred"] += 1
    return coverage


def _invoke_with_timeout_retry(
    llm: Any,
    prompt: str,
    *,
    settings: Settings,
    model_key: str,
    compact_prompt: str | None = None,
    state: GraphState | None = None,
    node_name: str = "repository_kb_distillation",
    schema_name: str = "",
    request_label: str = "primary",
    input_summary: Dict[str, Any] | None = None,
) -> tuple[object, bool, int, List[Dict[str, Any]]]:
    try:
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name=node_name,
            model_key=model_key,
            schema_name=schema_name,
            request_label=request_label,
            input_summary=input_summary,
        )
        return traced.result, False, traced.tokens, traced.trace_records
    except Exception as exc:
        if not (is_timeout_exception(exc) and is_local_model(model_key)):
            raise
        llm_trace = trace_from_exception(exc)
        active, _status = local_llm_server_active(settings)
        if not active:
            raise
        deadline = time.monotonic() + max(0, int(settings.semantic_agent_timeout_patience_seconds))
        sleep_for_retry(settings.semantic_agent_retry_backoff_seconds, 1, deadline)
        retry_prompt = compact_prompt or prompt
        traced = trace_llm_call(
            llm,
            retry_prompt,
            state=state,
            node_name=node_name,
            model_key=model_key,
            schema_name=schema_name,
            request_label=f"{request_label}:timeout_retry",
            input_summary=input_summary,
        )
        return traced.result, True, traced.tokens, llm_trace + traced.trace_records


def distill_repository_kb(
    bundle: ReviewKBBundle,
    *,
    settings: Settings | None = None,
    model_key: str | None = None,
    use_llm: bool = True,
    state: GraphState | None = None,
    llm_trace: List[Dict[str, Any]] | None = None,
) -> tuple[ReviewKBBundle, int, List[str]]:
    """Overlay deterministic KB summaries with bounded LLM-synthesized records."""
    resolved = settings or get_settings()
    if not use_llm or resolved.repository_kb_distillation_mode == "off":
        return bundle, 0, ["repository_kb_distillation:disabled"]

    warnings: List[str] = []
    tokens = 0
    selected = model_key or resolved.semantic_model_key
    cache_namespace = bundle.repo.id
    budget = _DistillationBudget(resolved)
    selected_communities = _selected_community_ids(bundle, resolved)
    community_fallbacks = {
        _metadata_int(r, "community_id"): r
        for r in bundle.summaries
        if r.metadata.get("summary_scope") == "community"
    }
    community_records = {_metadata_int(r, "community_id"): r for r in bundle.communities}
    replacements: List[ReviewKBRecord] = []
    planner = RepositoryKBDistillationPlanner(
        max_prompt_chars=int(resolved.repository_kb_distillation_max_prompt_chars),
        max_shards_per_community=int(resolved.repository_kb_distillation_max_shards_per_community),
    )

    for community in bundle.communities:
        cid = _metadata_int(community, "community_id")
        fallback = community_fallbacks.get(cid)
        if fallback is None:
            continue
        if cid not in selected_communities:
            replacements.append(_mark_summary_status(fallback, "not_scheduled", resolved.repository_kb_distillation_mode))
            continue
        plan = planner.plan(bundle, cid)
        if plan.mode == "direct" and plan.direct_pack is not None:
            record, call_tokens, plan_warnings = _distill_direct_community(
                pack=plan.direct_pack,
                fallback=fallback,
                settings=resolved,
                model_key=selected,
                telemetry=plan.telemetry,
                budget=budget,
                cache_namespace=cache_namespace,
                state=state,
                llm_trace=llm_trace,
            )
            replacements.append(_mark_distillation_scope(record, resolved.repository_kb_distillation_mode))
            tokens += call_tokens
            warnings.extend(plan_warnings)
            continue

        shard_records, shard_tokens, shard_warnings = _distill_shards(
            plan=plan,
            settings=resolved,
            model_key=selected,
            budget=budget,
            cache_namespace=cache_namespace,
            state=state,
            llm_trace=llm_trace,
        )
        replacements.extend(_mark_distillation_scope(r, resolved.repository_kb_distillation_mode) for r in shard_records)
        tokens += shard_tokens
        warnings.extend(shard_warnings)

        merge_record, merge_tokens, merge_warnings = _distill_community_from_shards(
            plan=plan,
            community_record=community_records[cid],
            fallback=fallback,
            shard_records=shard_records,
            settings=resolved,
            model_key=selected,
            budget=budget,
            cache_namespace=cache_namespace,
            state=state,
            llm_trace=llm_trace,
        )
        replacements.append(_mark_distillation_scope(merge_record, resolved.repository_kb_distillation_mode))
        tokens += merge_tokens
        warnings.extend(merge_warnings)

    distilled_summaries = replace_summary_records(bundle.summaries, replacements)
    bundle.summaries = distilled_summaries
    bundle.manifest.counts["summaries"] = len(bundle.summaries)

    repo_fallback = next((r for r in bundle.summaries if r.id == "summary:repo"), None)
    if repo_fallback is not None and selected_communities:
        pack = build_repo_distillation_pack(bundle)
        cached = _load_cached_record(
            settings=resolved,
            namespace=cache_namespace,
            kind="repo",
            model_key=model_key or resolved.semantic_merge_model_key,
            pack=pack,
        )
        if cached is not None:
            bundle.summaries = replace_summary_records(
                bundle.summaries,
                [_mark_distillation_scope(cached, resolved.repository_kb_distillation_mode)],
            )
            warnings.append("repo:distillation_cache_hit")
        else:
            prompt = render_repository_kb_repo_distill_prompt(
                pack_json=pack_to_prompt_json(pack),
            )
            allowed, estimate = budget.can_call(prompt, resolved.repository_kb_distillation_max_completion_tokens)
            if not allowed:
                warnings.append("repo:distillation_deferred:budget")
                bundle.summaries = replace_summary_records(
                    bundle.summaries,
                    [_mark_summary_status(repo_fallback, "budget_deferred", "token_budget")],
                )
            else:
                try:
                    llm = Models.worker(
                        RepositoryKBRepoDistillationOutput,
                        model_key=model_key or resolved.semantic_merge_model_key,
                        max_completion_tokens=resolved.repository_kb_distillation_max_completion_tokens,
                    )
                    invoke_result, retried, call_tokens, call_trace = _invoke_with_timeout_retry(
                        llm,
                        prompt,
                        settings=resolved,
                        model_key=model_key or resolved.semantic_merge_model_key,
                        compact_prompt=prompt + _GLOBAL_COMPACT_RETRY_APPENDIX,
                        state=state,
                        schema_name="RepositoryKBRepoDistillationOutput",
                        request_label="repo",
                    )
                    if llm_trace is not None:
                        llm_trace.extend(call_trace)
                    parsed = parse_structured_output(invoke_result, RepositoryKBRepoDistillationOutput)
                    budget.add(call_tokens, estimate)
                    tokens += call_tokens
                    repo_record = repo_summary_record_from_distillation(
                        fallback=repo_fallback,
                        output=parsed,
                        allowed_record_ids=pack.get("allowed_record_ids") or [],
                        omitted=pack.get("omitted") or {},
                        token_usage=call_tokens,
                    )
                    _save_cached_record(
                        repo_record,
                        settings=resolved,
                        namespace=cache_namespace,
                        kind="repo",
                        model_key=model_key or resolved.semantic_merge_model_key,
                        pack=pack,
                    )
                    bundle.summaries = replace_summary_records(
                        bundle.summaries,
                        [_mark_distillation_scope(repo_record, resolved.repository_kb_distillation_mode)],
                    )
                    warnings.extend(parsed.warnings)
                    if retried:
                        warnings.append("repo:distillation_retry:reason=timeout")
                except Exception as exc:  # noqa: BLE001
                    if llm_trace is not None:
                        llm_trace.extend(trace_from_exception(exc))
                    reason = exc.__class__.__name__
                    warnings.append(f"repo:distillation_failed:{reason}")
                    logger.warning("repository KB repo distillation failed err=%s", exc)
                    bundle.summaries = replace_summary_records(
                        bundle.summaries,
                        mark_distillation_failed([repo_fallback], reason),
                    )

    rebuild_review_kb_lexical_index(bundle)
    bundle.manifest.diagnostics["distillation_coverage"] = _record_distillation_coverage(bundle)
    bundle.manifest.diagnostics["distillation_mode"] = resolved.repository_kb_distillation_mode
    bundle.manifest.diagnostics["distillation_selected_communities"] = sorted(selected_communities)
    bundle.manifest.diagnostics["distillation_budget_tokens_used"] = budget.used
    return bundle, tokens, warnings


def _distill_direct_community(
    *,
    pack: Dict[str, Any],
    fallback: ReviewKBRecord,
    settings: Settings,
    model_key: str,
    telemetry: Dict[str, Any],
    budget: _DistillationBudget,
    cache_namespace: str,
    state: GraphState | None = None,
    llm_trace: List[Dict[str, Any]] | None = None,
) -> tuple[ReviewKBRecord, int, List[str]]:
    warnings: List[str] = []
    prompt_pack = {
        "prompt_version": "repository_kb_community_distill_v1",
        "communities": [pack],
    }
    cached = _load_cached_record(
        settings=settings,
        namespace=cache_namespace,
        kind="community_direct",
        model_key=model_key,
        pack=prompt_pack,
    )
    if cached is not None:
        cached.metadata["distillation_telemetry"] = dict(telemetry)
        return cached, 0, [f"community_{_metadata_int(fallback, 'community_id')}:distillation_cache_hit"]
    prompt = render_repository_kb_community_distill_prompt(
        pack_json=pack_to_prompt_json(prompt_pack),
    )
    allowed, estimate = budget.can_call(prompt, settings.repository_kb_distillation_max_completion_tokens)
    if not allowed:
        deferred = _mark_summary_status(fallback, "budget_deferred", "token_budget")
        deferred.metadata["distillation_telemetry"] = dict(telemetry)
        return deferred, 0, [f"community_{_metadata_int(fallback, 'community_id')}:distillation_deferred:budget"]
    try:
        llm = Models.worker(
            RepositoryKBCommunityDistillationOutput,
            model_key=model_key,
            max_completion_tokens=settings.repository_kb_distillation_max_completion_tokens,
        )
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="repository_kb_distillation",
            model_key=model_key,
            schema_name="RepositoryKBCommunityDistillationOutput",
            request_label=f"community_{_metadata_int(fallback, 'community_id')}:direct",
            input_summary={"community_id": _metadata_int(fallback, "community_id"), "mode": "direct"},
        )
        invoke_result = traced.result
        if llm_trace is not None:
            llm_trace.extend(traced.trace_records)
        parsed = parse_structured_output(invoke_result, RepositoryKBCommunityDistillationOutput)
        call_tokens = traced.tokens
        budget.add(call_tokens, estimate)
        cid = _metadata_int(fallback, "community_id")
        item = next((row for row in parsed.communities if int(row.community_id) == cid), None)
        if item is None:
            raise ValueError("community_distillation_missing_item")
        omitted = dict(pack.get("omitted") or {})
        omitted["mode"] = "direct"
        record = community_summary_record_from_distillation(
            fallback=fallback,
            item=item,
            allowed_record_ids=pack.get("allowed_record_ids") or [],
            omitted=omitted,
            token_usage=call_tokens,
        )
        record.metadata["distillation_telemetry"] = dict(telemetry)
        _save_cached_record(
            record,
            settings=settings,
            namespace=cache_namespace,
            kind="community_direct",
            model_key=model_key,
            pack=prompt_pack,
        )
        return record, call_tokens, list(parsed.warnings)
    except Exception as exc:  # noqa: BLE001
        if llm_trace is not None:
            llm_trace.extend(trace_from_exception(exc))
        reason = exc.__class__.__name__
        warnings.append(f"community_{_metadata_int(fallback, 'community_id')}:distillation_failed:{reason}")
        logger.warning("repository KB direct community distillation failed community=%s err=%s", fallback.id, exc)
        failed = mark_distillation_failed([fallback], reason)[0]
        failed.metadata["distillation_telemetry"] = dict(telemetry)
        return failed, 0, warnings


def _distill_shards(
    *,
    plan: RepositoryKBDistillationPlan,
    settings: Settings,
    model_key: str,
    budget: _DistillationBudget,
    cache_namespace: str,
    state: GraphState | None = None,
    llm_trace: List[Dict[str, Any]] | None = None,
) -> tuple[List[ReviewKBRecord], int, List[str]]:
    records: List[ReviewKBRecord] = []
    warnings: List[str] = []
    tokens = 0
    for shard in plan.shards:
        pack = shard_to_prompt_pack(shard)
        coverage = dict(pack.get("coverage") or {})
        coverage.update(
            {
                "prompt_chars": shard.prompt_chars,
                "estimated_prompt_tokens": shard.estimated_prompt_tokens,
                "omitted_record_ids": list(shard.omitted_record_ids),
            }
        )
        prompt_pack = {
            "prompt_version": "repository_kb_shard_distill_v1",
            "shards": [pack],
        }
        cached = _load_cached_record(
            settings=settings,
            namespace=cache_namespace,
            kind="community_shard",
            model_key=model_key,
            pack=prompt_pack,
        )
        if cached is not None:
            records.append(cached)
            warnings.append(f"community_{plan.community_id}:shard_{shard.shard_id}:cache_hit")
            continue
        prompt = render_repository_kb_shard_distill_prompt(
            pack_json=pack_to_prompt_json(prompt_pack),
        )
        allowed, estimate = budget.can_call(prompt, settings.repository_kb_distillation_max_completion_tokens)
        if not allowed:
            warnings.append(f"community_{plan.community_id}:shard_{shard.shard_id}:deferred:budget")
            records.append(
                fallback_shard_summary_record(
                    community_id=plan.community_id,
                    shard_id=shard.shard_id,
                    lane=shard.lane,
                    source_record_ids=shard.source_record_ids,
                    coverage=coverage,
                    reason="budget_deferred",
                )
            )
            records[-1].metadata["llm_distillation_status"] = "budget_deferred"
            continue
        try:
            invoke_result, retried, call_tokens, call_trace = _invoke_shard_distillation(
                prompt=prompt,
                settings=settings,
                model_key=model_key,
                budget=budget,
                estimate=estimate,
                state=state,
                request_label=f"community_{plan.community_id}:shard_{shard.shard_id}",
            )
            if llm_trace is not None:
                llm_trace.extend(call_trace)
            parsed = parse_structured_output(invoke_result, RepositoryKBShardDistillationOutput)
            budget.add(call_tokens, estimate)
            tokens += call_tokens
            item = next((row for row in parsed.shards if row.shard_id == shard.shard_id), None)
            if item is None:
                raise ValueError("shard_distillation_missing_item")
            record = shard_summary_record_from_distillation(
                item=item,
                allowed_record_ids=pack.get("allowed_record_ids") or [],
                coverage=coverage,
                token_usage=call_tokens,
            )
            _save_cached_record(
                record,
                settings=settings,
                namespace=cache_namespace,
                kind="community_shard",
                model_key=model_key,
                pack=prompt_pack,
            )
            records.append(record)
            warnings.extend(parsed.warnings)
            if retried:
                warnings.append(f"community_{plan.community_id}:shard_{shard.shard_id}:retry:reason=length")
        except Exception as exc:  # noqa: BLE001
            if llm_trace is not None:
                llm_trace.extend(trace_from_exception(exc))
            reason = exc.__class__.__name__
            warnings.append(f"community_{plan.community_id}:shard_{shard.shard_id}:failed:{reason}")
            logger.warning(
                "repository KB shard distillation failed community=%s shard=%s err=%s",
                plan.community_id,
                shard.shard_id,
                exc,
            )
            records.append(
                fallback_shard_summary_record(
                    community_id=plan.community_id,
                    shard_id=shard.shard_id,
                    lane=shard.lane,
                    source_record_ids=shard.source_record_ids,
                    coverage=coverage,
                    reason=reason,
                )
            )
    return records, tokens, warnings


def _invoke_shard_distillation(
    *,
    prompt: str,
    settings: Settings,
    model_key: str,
    budget: _DistillationBudget,
    estimate: int,
    state: GraphState | None = None,
    request_label: str = "shard",
) -> tuple[object, bool, int, List[Dict[str, Any]]]:
    llm = Models.worker(
        RepositoryKBShardDistillationOutput,
        model_key=model_key,
        max_completion_tokens=settings.repository_kb_distillation_max_completion_tokens,
    )
    try:
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="repository_kb_distillation",
            model_key=model_key,
            schema_name="RepositoryKBShardDistillationOutput",
            request_label=request_label,
        )
        return traced.result, False, traced.tokens, traced.trace_records
    except Exception as exc:
        if not _is_length_finish_error(exc):
            raise
        llm_trace = trace_from_exception(exc)
        budget.add(0, estimate)
        compact_llm = Models.worker(
            RepositoryKBShardDistillationOutput,
            model_key=model_key,
            max_completion_tokens=_compact_retry_completion_tokens(prompt, settings),
        )
        traced = trace_llm_call(
            compact_llm,
            prompt + _SHARD_COMPACT_RETRY_APPENDIX,
            state=state,
            node_name="repository_kb_distillation",
            model_key=model_key,
            schema_name="RepositoryKBShardDistillationOutput",
            request_label=f"{request_label}:compact_retry",
        )
        return traced.result, True, traced.tokens, llm_trace + traced.trace_records


def _distill_community_from_shards(
    *,
    plan: RepositoryKBDistillationPlan,
    community_record: ReviewKBRecord,
    fallback: ReviewKBRecord,
    shard_records: List[ReviewKBRecord],
    settings: Settings,
    model_key: str,
    budget: _DistillationBudget,
    cache_namespace: str,
    state: GraphState | None = None,
    llm_trace: List[Dict[str, Any]] | None = None,
) -> tuple[ReviewKBRecord, int, List[str]]:
    warnings: List[str] = []
    selected_shards = _fit_merge_shards(
        community_id=plan.community_id,
        community_record=community_record,
        shard_records=shard_records,
        max_chars=int(settings.repository_kb_distillation_shard_merge_max_prompt_chars),
        telemetry=plan.telemetry,
    )
    pack = community_merge_pack(
        community_id=plan.community_id,
        community_record=community_record,
        shard_records=selected_shards,
        telemetry=plan.telemetry,
    )
    cached = _load_cached_record(
        settings=settings,
        namespace=cache_namespace,
        kind="community_merge",
        model_key=model_key,
        pack=pack,
    )
    if cached is not None:
        cached.metadata["distillation_telemetry"] = dict(plan.telemetry)
        return cached, 0, [f"community_{plan.community_id}:merge_cache_hit"]
    prompt = render_repository_kb_community_merge_prompt(
        pack_json=pack_to_prompt_json(pack),
    )
    allowed, estimate = budget.can_call(prompt, settings.repository_kb_distillation_max_completion_tokens)
    if not allowed:
        deferred = _mark_summary_status(fallback, "budget_deferred", "token_budget")
        deferred.metadata["distillation_telemetry"] = dict(plan.telemetry)
        deferred.metadata["source_record_ids"] = [r.id for r in selected_shards] or fallback.metadata.get(
            "source_record_ids",
            [],
        )
        return deferred, 0, [f"community_{plan.community_id}:merge_deferred:budget"]
    try:
        llm = Models.worker(
            RepositoryKBCommunityDistillationOutput,
            model_key=model_key,
            max_completion_tokens=settings.repository_kb_distillation_max_completion_tokens,
        )
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="repository_kb_distillation",
            model_key=model_key,
            schema_name="RepositoryKBCommunityDistillationOutput",
            request_label=f"community_{plan.community_id}:merge",
            input_summary={"community_id": plan.community_id, "shard_count": len(selected_shards)},
        )
        invoke_result = traced.result
        if llm_trace is not None:
            llm_trace.extend(traced.trace_records)
        parsed = parse_structured_output(invoke_result, RepositoryKBCommunityDistillationOutput)
        call_tokens = traced.tokens
        budget.add(call_tokens, estimate)
        item = next((row for row in parsed.communities if int(row.community_id) == plan.community_id), None)
        if item is None:
            raise ValueError("community_merge_missing_item")
        omitted = dict(plan.telemetry.get("omitted") or {})
        omitted["mode"] = "sharded"
        record = community_summary_record_from_distillation(
            fallback=fallback,
            item=item,
            allowed_record_ids=pack.get("allowed_record_ids") or [],
            omitted=omitted,
            token_usage=call_tokens,
        )
        record.metadata["distillation_telemetry"] = dict(plan.telemetry)
        _save_cached_record(
            record,
            settings=settings,
            namespace=cache_namespace,
            kind="community_merge",
            model_key=model_key,
            pack=pack,
        )
        return record, call_tokens, list(parsed.warnings)
    except Exception as exc:  # noqa: BLE001
        if llm_trace is not None:
            llm_trace.extend(trace_from_exception(exc))
        reason = exc.__class__.__name__
        warnings.append(f"community_{plan.community_id}:merge_failed:{reason}")
        logger.warning("repository KB community shard merge failed community=%s err=%s", plan.community_id, exc)
        failed = mark_distillation_failed([fallback], reason)[0]
        failed.metadata["distillation_telemetry"] = dict(plan.telemetry)
        failed.metadata["source_record_ids"] = [r.id for r in selected_shards] or fallback.metadata.get(
            "source_record_ids",
            [],
        )
        return failed, 0, warnings


def _fit_merge_shards(
    *,
    community_id: int,
    community_record: ReviewKBRecord,
    shard_records: List[ReviewKBRecord],
    max_chars: int,
    telemetry: Dict[str, Any],
) -> List[ReviewKBRecord]:
    selected: List[ReviewKBRecord] = []
    omitted: List[str] = []
    for record in shard_records:
        candidate = [*selected, record]
        pack = community_merge_pack(
            community_id=community_id,
            community_record=community_record,
            shard_records=candidate,
            telemetry=telemetry,
        )
        if selected and len(pack_to_prompt_json(pack)) > max_chars:
            omitted.append(record.id)
            continue
        selected = candidate
    if omitted:
        telemetry["omitted_shard_summary_ids"] = omitted
    return selected


def _bounded_batches(
    packs: List[Dict[str, Any]],
    *,
    max_items: int,
    max_chars: int,
) -> List[List[Dict[str, Any]]]:
    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for pack in packs:
        candidate = [*current, pack]
        prompt_len = len(
            pack_to_prompt_json(
                {
                    "prompt_version": "repository_kb_community_distill_v1",
                    "communities": candidate,
                }
            )
        )
        if current and (len(candidate) > max_items or prompt_len > max_chars):
            batches.append(current)
            current = [pack]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def _pack_community_id(pack: Dict[str, Any]) -> int:
    value = pack.get("community_id")
    if value is None:
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
