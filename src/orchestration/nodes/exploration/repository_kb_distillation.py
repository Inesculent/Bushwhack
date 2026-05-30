"""LLM-backed, bounded distillation over Repository KB records."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.config import Settings, get_settings
from src.domain.schemas import (
    RepositoryKBCommunityDistillationOutput,
    RepositoryKBRepoDistillationOutput,
    RepositoryKBShardDistillationOutput,
    ReviewKBRecord,
)
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
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


def distill_repository_kb(
    bundle: ReviewKBBundle,
    *,
    settings: Settings | None = None,
    model_key: str | None = None,
    use_llm: bool = True,
) -> tuple[ReviewKBBundle, int, List[str]]:
    """Overlay deterministic KB summaries with bounded LLM-synthesized records."""
    resolved = settings or get_settings()
    if not use_llm:
        return bundle, 0, ["repository_kb_distillation:disabled"]

    warnings: List[str] = []
    tokens = 0
    selected = model_key or resolved.semantic_model_key
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
        plan = planner.plan(bundle, cid)
        if plan.mode == "direct" and plan.direct_pack is not None:
            record, call_tokens, plan_warnings = _distill_direct_community(
                pack=plan.direct_pack,
                fallback=fallback,
                settings=resolved,
                model_key=selected,
                telemetry=plan.telemetry,
            )
            replacements.append(record)
            tokens += call_tokens
            warnings.extend(plan_warnings)
            continue

        shard_records, shard_tokens, shard_warnings = _distill_shards(
            plan=plan,
            settings=resolved,
            model_key=selected,
        )
        replacements.extend(shard_records)
        tokens += shard_tokens
        warnings.extend(shard_warnings)

        merge_record, merge_tokens, merge_warnings = _distill_community_from_shards(
            plan=plan,
            community_record=community_records[cid],
            fallback=fallback,
            shard_records=shard_records,
            settings=resolved,
            model_key=selected,
        )
        replacements.append(merge_record)
        tokens += merge_tokens
        warnings.extend(merge_warnings)

    distilled_summaries = replace_summary_records(bundle.summaries, replacements)
    bundle.summaries = distilled_summaries
    bundle.manifest.counts["summaries"] = len(bundle.summaries)

    repo_fallback = next((r for r in bundle.summaries if r.id == "summary:repo"), None)
    if repo_fallback is not None:
        pack = build_repo_distillation_pack(bundle)
        try:
            llm = Models.worker(
                RepositoryKBRepoDistillationOutput,
                model_key=model_key or resolved.semantic_merge_model_key,
                max_completion_tokens=resolved.repository_kb_distillation_max_completion_tokens,
            )
            invoke_result = llm.invoke(
                render_repository_kb_repo_distill_prompt(
                    pack_json=pack_to_prompt_json(pack),
                )
            )
            parsed = parse_structured_output(invoke_result, RepositoryKBRepoDistillationOutput)
            call_tokens = extract_total_tokens_from_llm_result(invoke_result)
            tokens += call_tokens
            bundle.summaries = replace_summary_records(
                bundle.summaries,
                [
                    repo_summary_record_from_distillation(
                        fallback=repo_fallback,
                        output=parsed,
                        allowed_record_ids=pack.get("allowed_record_ids") or [],
                        omitted=pack.get("omitted") or {},
                        token_usage=call_tokens,
                    )
                ],
            )
            warnings.extend(parsed.warnings)
        except Exception as exc:  # noqa: BLE001
            reason = exc.__class__.__name__
            warnings.append(f"repo:distillation_failed:{reason}")
            logger.warning("repository KB repo distillation failed err=%s", exc)
            bundle.summaries = replace_summary_records(
                bundle.summaries,
                mark_distillation_failed([repo_fallback], reason),
            )

    rebuild_review_kb_lexical_index(bundle)
    return bundle, tokens, warnings


def _distill_direct_community(
    *,
    pack: Dict[str, Any],
    fallback: ReviewKBRecord,
    settings: Settings,
    model_key: str,
    telemetry: Dict[str, Any],
) -> tuple[ReviewKBRecord, int, List[str]]:
    warnings: List[str] = []
    try:
        llm = Models.worker(
            RepositoryKBCommunityDistillationOutput,
            model_key=model_key,
            max_completion_tokens=settings.repository_kb_distillation_max_completion_tokens,
        )
        invoke_result = llm.invoke(
            render_repository_kb_community_distill_prompt(
                pack_json=pack_to_prompt_json(
                    {
                        "prompt_version": "repository_kb_community_distill_v1",
                        "communities": [pack],
                    }
                ),
            )
        )
        parsed = parse_structured_output(invoke_result, RepositoryKBCommunityDistillationOutput)
        call_tokens = extract_total_tokens_from_llm_result(invoke_result)
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
        return record, call_tokens, list(parsed.warnings)
    except Exception as exc:  # noqa: BLE001
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
        try:
            invoke_result, retried = _invoke_shard_distillation(
                pack=pack,
                settings=settings,
                model_key=model_key,
            )
            parsed = parse_structured_output(invoke_result, RepositoryKBShardDistillationOutput)
            call_tokens = extract_total_tokens_from_llm_result(invoke_result)
            tokens += call_tokens
            item = next((row for row in parsed.shards if row.shard_id == shard.shard_id), None)
            if item is None:
                raise ValueError("shard_distillation_missing_item")
            records.append(
                shard_summary_record_from_distillation(
                    item=item,
                    allowed_record_ids=pack.get("allowed_record_ids") or [],
                    coverage=coverage,
                    token_usage=call_tokens,
                )
            )
            warnings.extend(parsed.warnings)
            if retried:
                warnings.append(f"community_{plan.community_id}:shard_{shard.shard_id}:retry:reason=length")
        except Exception as exc:  # noqa: BLE001
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
    pack: Dict[str, Any],
    settings: Settings,
    model_key: str,
) -> tuple[object, bool]:
    llm = Models.worker(
        RepositoryKBShardDistillationOutput,
        model_key=model_key,
        max_completion_tokens=settings.repository_kb_distillation_max_completion_tokens,
    )
    prompt = render_repository_kb_shard_distill_prompt(
        pack_json=pack_to_prompt_json(
            {
                "prompt_version": "repository_kb_shard_distill_v1",
                "shards": [pack],
            }
        ),
    )
    try:
        return llm.invoke(prompt), False
    except Exception as exc:
        if not _is_length_finish_error(exc):
            raise
        compact_llm = Models.worker(
            RepositoryKBShardDistillationOutput,
            model_key=model_key,
            max_completion_tokens=min(
                settings.repository_kb_distillation_max_completion_tokens,
                1024,
            ),
        )
        return compact_llm.invoke(prompt + _SHARD_COMPACT_RETRY_APPENDIX), True


def _distill_community_from_shards(
    *,
    plan: RepositoryKBDistillationPlan,
    community_record: ReviewKBRecord,
    fallback: ReviewKBRecord,
    shard_records: List[ReviewKBRecord],
    settings: Settings,
    model_key: str,
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
    try:
        llm = Models.worker(
            RepositoryKBCommunityDistillationOutput,
            model_key=model_key,
            max_completion_tokens=settings.repository_kb_distillation_max_completion_tokens,
        )
        invoke_result = llm.invoke(
            render_repository_kb_community_merge_prompt(
                pack_json=pack_to_prompt_json(pack),
            )
        )
        parsed = parse_structured_output(invoke_result, RepositoryKBCommunityDistillationOutput)
        call_tokens = extract_total_tokens_from_llm_result(invoke_result)
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
        return record, call_tokens, list(parsed.warnings)
    except Exception as exc:  # noqa: BLE001
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
