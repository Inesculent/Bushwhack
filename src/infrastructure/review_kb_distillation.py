"""Bounded evidence packs and summary-record helpers for Repository KB distillation."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from src.domain.schemas import (
    RepositoryKBCommunityDistillationItem,
    RepositoryKBRepoDistillationOutput,
    RepositoryKBShardDistillationItem,
    ReviewKBRecord,
)
from src.infrastructure.review_kb import ReviewKBBundle

COMMUNITY_DISTILL_PROMPT_VERSION = "repository_kb_community_distill_v1"
COMMUNITY_MERGE_PROMPT_VERSION = "repository_kb_community_merge_v1"
REPO_DISTILL_PROMPT_VERSION = "repository_kb_repo_distill_v1"
SHARD_DISTILL_PROMPT_VERSION = "repository_kb_shard_distill_v1"


@dataclass(frozen=True)
class RepositoryKBDistillationShard:
    community_id: int
    shard_id: str
    lane: str
    records: List[ReviewKBRecord]
    total_lane_records: int
    prompt_chars: int
    estimated_prompt_tokens: int
    omitted_record_ids: List[str]

    @property
    def source_record_ids(self) -> List[str]:
        return [record.id for record in self.records]


@dataclass(frozen=True)
class RepositoryKBDistillationPlan:
    community_id: int
    mode: str
    direct_pack: Dict[str, Any] | None
    shards: List[RepositoryKBDistillationShard]
    telemetry: Dict[str, Any]


class RepositoryKBDistillationPlanner:
    """Plan direct or sharded Repository KB distillation before any LLM call."""

    def __init__(
        self,
        *,
        max_prompt_chars: int,
        max_shards_per_community: int,
    ) -> None:
        self.max_prompt_chars = max(1000, int(max_prompt_chars))
        self.max_shards_per_community = max(1, int(max_shards_per_community))

    def plan(self, bundle: ReviewKBBundle, community_id: int) -> RepositoryKBDistillationPlan:
        full_pack = build_community_distillation_pack(
            bundle,
            community_id,
            max_files=10_000,
            max_symbols=10_000,
            max_facts=10_000,
            max_edges=10_000,
        )
        direct_chars = _prompt_chars_for_community_pack(full_pack)
        if direct_chars <= self.max_prompt_chars:
            return RepositoryKBDistillationPlan(
                community_id=community_id,
                mode="direct",
                direct_pack=full_pack,
                shards=[],
                telemetry={
                    "mode": "direct",
                    "prompt_chars": direct_chars,
                    "estimated_prompt_tokens": _estimate_tokens(direct_chars),
                    "total_records": len(full_pack.get("records") or []),
                    "selected_records": len(full_pack.get("allowed_record_ids") or []),
                    "shard_count": 0,
                    "omitted": dict(full_pack.get("omitted") or {}),
                },
            )

        lane_records = _community_lane_records(bundle, community_id)
        shards: List[RepositoryKBDistillationShard] = []
        for lane, records in lane_records.items():
            shards.extend(
                _split_lane_into_shards(
                    community_id=community_id,
                    lane=lane,
                    records=records,
                    max_prompt_chars=self.max_prompt_chars,
                )
            )

        skipped_lanes: List[str] = []
        omitted_record_ids: List[str] = []
        if len(shards) > self.max_shards_per_community:
            kept = shards[: self.max_shards_per_community]
            dropped = shards[self.max_shards_per_community :]
            skipped_lanes = sorted({s.lane for s in dropped})
            for shard in dropped:
                omitted_record_ids.extend(shard.source_record_ids)
            shards = kept

        return RepositoryKBDistillationPlan(
            community_id=community_id,
            mode="sharded",
            direct_pack=None,
            shards=shards,
            telemetry={
                "mode": "sharded",
                "prompt_chars": direct_chars,
                "estimated_prompt_tokens": _estimate_tokens(direct_chars),
                "total_records": len(full_pack.get("records") or []),
                "selected_records": sum(len(s.records) for s in shards),
                "shard_count": len(shards),
                "skipped_lanes": skipped_lanes,
                "omitted_record_ids": omitted_record_ids,
                "omitted": dict(full_pack.get("omitted") or {}),
            },
        )


def _record_pack(record: ReviewKBRecord) -> Dict[str, Any]:
    evidence_refs = []
    for ev in record.evidence[:2]:
        ref = ev.file_path or ev.graph_node_id or ev.note
        if ev.line_start:
            ref = f"{ref}:{ev.line_start}"
        if ref:
            evidence_refs.append(ref)
    return {
        "id": record.id,
        "kind": record.kind,
        "summary": record.summary[:260],
        "confidence": record.confidence,
        "tags": list(record.tags[:8]),
        "evidence_refs": evidence_refs,
        "metadata": _safe_metadata(record),
    }


def _safe_metadata(record: ReviewKBRecord) -> Dict[str, Any]:
    meta = dict(record.metadata)
    allowed = {
        "community_id",
        "file_path",
        "symbol_name",
        "qualified_name",
        "symbol_type",
        "signature",
        "cross_community_dependencies",
        "source_record_ids",
        "summary_scope",
        "omitted_files",
        "omitted_symbols",
        "omitted_facts",
    }
    compact = {k: v for k, v in meta.items() if k in allowed}
    if isinstance(compact.get("signature"), str):
        compact["signature"] = compact["signature"][:180]
    if isinstance(compact.get("cross_community_dependencies"), list):
        compact["cross_community_dependencies"] = compact["cross_community_dependencies"][:12]
    if isinstance(compact.get("source_record_ids"), list):
        compact["source_record_ids"] = compact["source_record_ids"][:12]
    return compact


def _metadata_int(record: ReviewKBRecord, key: str, default: int = -1) -> int:
    value = record.metadata.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_records(records: Iterable[ReviewKBRecord], limit: int) -> List[ReviewKBRecord]:
    out: List[ReviewKBRecord] = []
    seen: set[str] = set()
    for record in records:
        if record.id in seen:
            continue
        seen.add(record.id)
        out.append(record)
        if len(out) >= limit:
            break
    return out


def _estimate_tokens(chars: int) -> int:
    return max(1, int(chars / 4))


def _prompt_chars_for_community_pack(pack: Mapping[str, Any]) -> int:
    return len(pack_to_prompt_json({"prompt_version": COMMUNITY_DISTILL_PROMPT_VERSION, "communities": [pack]}))


def _prompt_chars_for_shard_pack(pack: Mapping[str, Any]) -> int:
    return len(pack_to_prompt_json({"prompt_version": SHARD_DISTILL_PROMPT_VERSION, "shards": [pack]}))


def _community_lane_records(bundle: ReviewKBBundle, community_id: int) -> Dict[str, List[ReviewKBRecord]]:
    community = next(
        (r for r in bundle.communities if _metadata_int(r, "community_id") == community_id),
        None,
    )
    if community is None:
        return {}

    file_by_path = {str(r.metadata.get("file_path") or ""): r for r in bundle.files}
    symbol_by_node = {
        str(r.metadata.get("symbol_node_id") or ""): r
        for r in bundle.symbols
        if r.metadata.get("symbol_node_id")
    }
    facts_by_path: Dict[str, List[ReviewKBRecord]] = defaultdict(list)
    for fact in bundle.facts:
        facts_by_path[str(fact.metadata.get("file_path") or "")].append(fact)

    paths = [str(p) for p in community.metadata.get("files") or []]
    bridge_nodes = [str(s) for s in community.metadata.get("bridge_symbols") or []]
    symbol_nodes = [str(s) for s in community.metadata.get("symbols") or []]
    bridge_symbols = [symbol_by_node[s] for s in bridge_nodes if s in symbol_by_node]
    bridge_ids = {r.id for r in bridge_symbols}
    internal_symbols = [
        symbol_by_node[s]
        for s in symbol_nodes
        if s in symbol_by_node and symbol_by_node[s].id not in bridge_ids
    ]
    files = [file_by_path[p] for p in paths if p in file_by_path]
    facts: List[ReviewKBRecord] = []
    for path in paths:
        facts.extend(
            sorted(
                facts_by_path.get(path, []),
                key=lambda r: (0 if {"contract", "tensor-shape", "signature"} & set(r.tags) else 1, r.id),
            )
        )
    symbol_record_ids = {r.id for r in [*bridge_symbols, *internal_symbols]}
    edges = [
        edge
        for edge in bundle.edges
        if edge.metadata.get("source_record_id") in symbol_record_ids
        or edge.metadata.get("target_record_id") in symbol_record_ids
    ]
    return {
        "overview_files": [community, *files],
        "bridge_symbols": bridge_symbols,
        "contracts_facts": facts,
        "dependency_edges": edges,
        "internal_symbols": internal_symbols,
    }


def _split_lane_into_shards(
    *,
    community_id: int,
    lane: str,
    records: Sequence[ReviewKBRecord],
    max_prompt_chars: int,
) -> List[RepositoryKBDistillationShard]:
    shards: List[RepositoryKBDistillationShard] = []
    current: List[ReviewKBRecord] = []
    chunk_idx = 0
    for record in records:
        candidate = [*current, record]
        pack = _shard_pack_dict(
            community_id=community_id,
            shard_id=f"{lane}:{chunk_idx}",
            lane=lane,
            records=candidate,
            total_lane_records=len(records),
        )
        if current and _prompt_chars_for_shard_pack(pack) > max_prompt_chars:
            shards.append(
                _make_shard(
                    community_id=community_id,
                    shard_id=f"{lane}:{chunk_idx}",
                    lane=lane,
                    records=current,
                    total_lane_records=len(records),
                    max_prompt_chars=max_prompt_chars,
                )
            )
            chunk_idx += 1
            current = [record]
        else:
            current = candidate
    if current:
        shards.append(
            _make_shard(
                community_id=community_id,
                shard_id=f"{lane}:{chunk_idx}",
                lane=lane,
                records=current,
                total_lane_records=len(records),
                max_prompt_chars=max_prompt_chars,
            )
        )
    return shards


def _make_shard(
    *,
    community_id: int,
    shard_id: str,
    lane: str,
    records: Sequence[ReviewKBRecord],
    total_lane_records: int,
    max_prompt_chars: int,
) -> RepositoryKBDistillationShard:
    pack = _shard_pack_dict(
        community_id=community_id,
        shard_id=shard_id,
        lane=lane,
        records=records,
        total_lane_records=total_lane_records,
    )
    prompt_chars = _prompt_chars_for_shard_pack(pack)
    omitted_record_ids: List[str] = []
    if prompt_chars > max_prompt_chars:
        omitted_record_ids = [r.id for r in records[1:]]
    return RepositoryKBDistillationShard(
        community_id=community_id,
        shard_id=shard_id,
        lane=lane,
        records=list(records),
        total_lane_records=total_lane_records,
        prompt_chars=prompt_chars,
        estimated_prompt_tokens=_estimate_tokens(prompt_chars),
        omitted_record_ids=omitted_record_ids,
    )


def _shard_pack_dict(
    *,
    community_id: int,
    shard_id: str,
    lane: str,
    records: Sequence[ReviewKBRecord],
    total_lane_records: int,
) -> Dict[str, Any]:
    return {
        "community_id": community_id,
        "shard_id": shard_id,
        "lane": lane,
        "prompt_version": SHARD_DISTILL_PROMPT_VERSION,
        "records": [_record_pack(r) for r in records],
        "allowed_record_ids": [r.id for r in records],
        "coverage": {
            "lane": lane,
            "total_lane_records": total_lane_records,
            "selected_records": len(records),
            "omitted_lane_records": max(0, total_lane_records - len(records)),
        },
    }


def shard_to_prompt_pack(shard: RepositoryKBDistillationShard) -> Dict[str, Any]:
    return _shard_pack_dict(
        community_id=shard.community_id,
        shard_id=shard.shard_id,
        lane=shard.lane,
        records=shard.records,
        total_lane_records=shard.total_lane_records,
    )


def community_merge_pack(
    *,
    community_id: int,
    community_record: ReviewKBRecord,
    shard_records: Sequence[ReviewKBRecord],
    telemetry: Mapping[str, Any],
) -> Dict[str, Any]:
    records = [community_record, *shard_records]
    return {
        "community_id": community_id,
        "prompt_version": COMMUNITY_MERGE_PROMPT_VERSION,
        "records": [_record_pack(r) for r in records],
        "allowed_record_ids": [r.id for r in records],
        "telemetry": _compact_merge_telemetry(telemetry),
    }


def _compact_merge_telemetry(telemetry: Mapping[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for key in (
        "mode",
        "prompt_chars",
        "estimated_prompt_tokens",
        "total_records",
        "selected_records",
        "shard_count",
    ):
        if key in telemetry:
            compact[key] = telemetry[key]

    skipped_lanes = telemetry.get("skipped_lanes")
    if isinstance(skipped_lanes, Sequence) and not isinstance(skipped_lanes, (str, bytes)):
        compact["skipped_lanes"] = [str(lane) for lane in skipped_lanes[:8]]
        if len(skipped_lanes) > 8:
            compact["skipped_lane_count"] = len(skipped_lanes)

    omitted_ids = telemetry.get("omitted_record_ids")
    if isinstance(omitted_ids, Sequence) and not isinstance(omitted_ids, (str, bytes)):
        compact["omitted_record_count"] = len(omitted_ids)
        compact["omitted_record_ids_sample"] = [str(record_id) for record_id in omitted_ids[:12]]

    omitted = telemetry.get("omitted")
    if isinstance(omitted, Mapping):
        compact["omitted"] = {
            str(key): value
            for key, value in omitted.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    return compact


def build_community_distillation_pack(
    bundle: ReviewKBBundle,
    community_id: int,
    *,
    max_files: int = 4,
    max_symbols: int = 8,
    max_facts: int = 6,
    max_edges: int = 4,
) -> Dict[str, Any]:
    """Build a repository-scoped community pack with hard record caps."""
    community = next(
        (r for r in bundle.communities if _metadata_int(r, "community_id") == community_id),
        None,
    )
    if community is None:
        return {"community_id": community_id, "records": [], "allowed_record_ids": []}

    file_by_path = {str(r.metadata.get("file_path") or ""): r for r in bundle.files}
    symbol_by_node = {
        str(r.metadata.get("symbol_node_id") or ""): r
        for r in bundle.symbols
        if r.metadata.get("symbol_node_id")
    }
    facts_by_path: Dict[str, List[ReviewKBRecord]] = defaultdict(list)
    for fact in bundle.facts:
        facts_by_path[str(fact.metadata.get("file_path") or "")].append(fact)

    paths = [str(p) for p in community.metadata.get("files") or []]
    bridge_nodes = [str(s) for s in community.metadata.get("bridge_symbols") or []]
    symbol_nodes = list(dict.fromkeys(bridge_nodes + [str(s) for s in community.metadata.get("symbols") or []]))
    files = _first_records((file_by_path[p] for p in paths if p in file_by_path), max_files)
    symbols = _first_records((symbol_by_node[s] for s in symbol_nodes if s in symbol_by_node), max_symbols)

    fact_candidates: List[ReviewKBRecord] = []
    for path in paths:
        tagged = sorted(
            facts_by_path.get(path, []),
            key=lambda r: (0 if {"contract", "tensor-shape", "signature"} & set(r.tags) else 1, r.id),
        )
        fact_candidates.extend(tagged[:3])
    facts = _first_records(fact_candidates, max_facts)

    symbol_record_ids = {r.id for r in symbols}
    edges = _first_records(
        (
            edge
            for edge in bundle.edges
            if edge.metadata.get("source_record_id") in symbol_record_ids
            or edge.metadata.get("target_record_id") in symbol_record_ids
        ),
        max_edges,
    )

    records = [community, *files, *symbols, *facts, *edges]
    return {
        "community_id": community_id,
        "prompt_version": COMMUNITY_DISTILL_PROMPT_VERSION,
        "records": [_record_pack(r) for r in records],
        "allowed_record_ids": [r.id for r in records],
        "omitted": {
            "files": max(0, len(paths) - len(files)),
            "symbols": max(0, len(symbol_nodes) - len(symbols)),
            "facts": max(0, sum(len(facts_by_path.get(p, [])) for p in paths) - len(facts)),
            "edges": max(0, len(bundle.edges) - len(edges)),
        },
    }


def build_repo_distillation_pack(
    bundle: ReviewKBBundle,
    *,
    max_community_summaries: int = 24,
    max_shard_summaries: int = 16,
    max_facts: int = 16,
    docs_summary: str = "",
    docs_sources: Sequence[str] = (),
) -> Dict[str, Any]:
    community_summaries = [
        r
        for r in bundle.summaries
        if r.kind == "summary" and r.metadata.get("summary_scope") == "community"
    ][:max_community_summaries]
    shard_summaries = [
        r
        for r in bundle.summaries
        if r.kind == "summary"
        and r.metadata.get("summary_scope") == "community_shard"
        and {"contract", "risk-surface"} & set(r.tags)
    ][:max_shard_summaries]
    facts = sorted(
        bundle.facts,
        key=lambda r: (0 if {"contract", "tensor-shape", "signature", "config"} & set(r.tags) else 1, r.id),
    )[:max_facts]
    topology = next((r for r in bundle.summaries if r.id == "summary:repo:topology"), None)
    records = [bundle.repo, *([topology] if topology is not None else []), *community_summaries, *shard_summaries, *facts]
    return {
        "prompt_version": REPO_DISTILL_PROMPT_VERSION,
        "records": [_record_pack(r) for r in records],
        "allowed_record_ids": [r.id for r in records],
        "docs_context": {
            "summary": str(docs_summary or "")[:4000],
            "allowed_doc_source_ids": [str(s) for s in docs_sources if str(s).startswith("doc:")][:20],
        },
        "omitted": {
            "community_summaries": max(
                0,
                len([r for r in bundle.summaries if r.metadata.get("summary_scope") == "community"])
                - len(community_summaries),
            ),
            "shard_summaries": max(
                0,
                len([r for r in bundle.summaries if r.metadata.get("summary_scope") == "community_shard"])
                - len(shard_summaries),
            ),
            "facts": max(0, len(bundle.facts) - len(facts)),
        },
    }


def pack_to_prompt_json(pack: Mapping[str, Any]) -> str:
    return json.dumps(pack, sort_keys=True, indent=2)


def community_summary_record_from_distillation(
    *,
    fallback: ReviewKBRecord,
    item: RepositoryKBCommunityDistillationItem,
    allowed_record_ids: Sequence[str],
    omitted: Mapping[str, int],
    token_usage: int = 0,
) -> ReviewKBRecord:
    allowed = set(allowed_record_ids)
    cited = [rid for rid in item.source_record_ids if rid in allowed]
    warnings = []
    if not cited:
        cited = [fallback.id]
        warnings.append("distillation_uncited:fallback_to_community_record")
    invalid = [rid for rid in item.source_record_ids if rid not in allowed]
    if invalid:
        warnings.append(f"distillation_invalid_citations:{len(invalid)}")
    parts = [
        item.purpose.strip(),
        _bullets("Responsibilities", item.responsibilities),
        _bullets("Public contracts", item.public_contracts),
        _bullets("Bridge symbols", item.bridge_symbols),
        _bullets("Important facts", item.important_facts),
        _bullets("Data shapes and config", item.data_shape_notes),
        _bullets("Risk surfaces", item.risk_surfaces),
        _bullets("Uncertainties", item.uncertainties),
        _bullets("Retrieval hints", item.retrieval_hints),
    ]
    summary = "\n".join(p for p in parts if p).strip() or fallback.summary
    return fallback.model_copy(
        update={
            "summary": summary,
            "confidence": "llm_synthesized",
            "tags": sorted(
                set(
                    [
                        *fallback.tags,
                        "repo-summary",
                        "community-summary",
                        "contract",
                        "bridge",
                        "risk-surface",
                    ]
                )
            ),
            "metadata": {
                **fallback.metadata,
                "label": item.label or fallback.metadata.get("label", ""),
                "source_record_ids": cited,
                "prompt_version": COMMUNITY_DISTILL_PROMPT_VERSION,
                "llm_distillation_status": "ok",
                "llm_distillation_warnings": warnings,
                "token_usage": token_usage,
                "omitted_files": int(omitted.get("files") or 0),
                "omitted_symbols": int(omitted.get("symbols") or 0),
                "omitted_facts": int(omitted.get("facts") or 0),
                "omitted_edges": int(omitted.get("edges") or 0),
                "distillation_mode": omitted.get("mode", ""),
            },
        }
    )


def shard_summary_record_from_distillation(
    *,
    item: RepositoryKBShardDistillationItem,
    allowed_record_ids: Sequence[str],
    coverage: Mapping[str, Any],
    token_usage: int = 0,
) -> ReviewKBRecord:
    allowed = set(allowed_record_ids)
    cited = [rid for rid in item.source_record_ids if rid in allowed]
    warnings = []
    if not cited:
        cited = list(allowed_record_ids[:8])
        warnings.append("distillation_uncited:fallback_to_shard_records")
    invalid = [rid for rid in item.source_record_ids if rid not in allowed]
    if invalid:
        warnings.append(f"distillation_invalid_citations:{len(invalid)}")
    parts = [
        item.summary.strip(),
        _bullets("Responsibilities", item.responsibilities),
        _bullets("Public contracts", item.public_contracts),
        _bullets("Important facts", item.important_facts),
        _bullets("Data shapes and config", item.data_shape_notes),
        _bullets("Risk surfaces", item.risk_surfaces),
        _bullets("Uncertainties", item.uncertainties),
        _bullets("Retrieval hints", item.retrieval_hints),
    ]
    summary = "\n".join(p for p in parts if p).strip()
    return ReviewKBRecord(
        id=f"summary:community:{item.community_id}:shard:{item.shard_id}",
        kind="summary",
        summary=summary or f"Community {item.community_id} shard {item.shard_id} ({item.lane}).",
        evidence=[],
        confidence="llm_synthesized",
        tags=sorted(set(["summary", "community-shard", item.lane, "contract", "risk-surface"])),
        metadata={
            "summary_scope": "community_shard",
            "community_id": item.community_id,
            "shard_id": item.shard_id,
            "lane": item.lane,
            "source_record_ids": cited,
            "coverage": dict(coverage),
            "prompt_version": SHARD_DISTILL_PROMPT_VERSION,
            "llm_distillation_status": "ok",
            "llm_distillation_warnings": warnings,
            "token_usage": token_usage,
        },
    )


def fallback_shard_summary_record(
    *,
    community_id: int,
    shard_id: str,
    lane: str,
    source_record_ids: Sequence[str],
    coverage: Mapping[str, Any],
    reason: str,
) -> ReviewKBRecord:
    return ReviewKBRecord(
        id=f"summary:community:{community_id}:shard:{shard_id}",
        kind="summary",
        summary=(
            f"Community {community_id} shard {shard_id} ({lane}) covers "
            f"{len(source_record_ids)} KB records; LLM distillation failed with {reason}."
        ),
        evidence=[],
        confidence="inferred",
        tags=sorted(set(["summary", "community-shard", lane])),
        metadata={
            "summary_scope": "community_shard",
            "community_id": community_id,
            "shard_id": shard_id,
            "lane": lane,
            "source_record_ids": list(source_record_ids),
            "coverage": dict(coverage),
            "prompt_version": SHARD_DISTILL_PROMPT_VERSION,
            "llm_distillation_status": "failed",
            "llm_distillation_error": reason,
        },
    )


def repo_summary_record_from_distillation(
    *,
    fallback: ReviewKBRecord,
    output: RepositoryKBRepoDistillationOutput,
    allowed_record_ids: Sequence[str],
    allowed_doc_source_ids: Sequence[str] = (),
    omitted: Mapping[str, int],
    token_usage: int = 0,
) -> ReviewKBRecord:
    allowed = set(allowed_record_ids)
    cited = [rid for rid in output.source_record_ids if rid in allowed]
    allowed_docs = set(allowed_doc_source_ids)
    cited_docs = [rid for rid in output.doc_source_ids if rid in allowed_docs]
    warnings = list(output.warnings)
    if not cited:
        cited = [fallback.id]
        warnings.append("distillation_uncited:fallback_to_repo_record")
    invalid = [rid for rid in output.source_record_ids if rid not in allowed]
    if invalid:
        warnings.append(f"distillation_invalid_citations:{len(invalid)}")
    invalid_docs = [rid for rid in output.doc_source_ids if rid not in allowed_docs]
    if invalid_docs:
        warnings.append(f"distillation_invalid_doc_citations:{len(invalid_docs)}")
    parts = [
        _section("Repository Understanding", output.what_it_is or output.summary),
        _bullets("Core workflows", output.core_workflows),
        _bullets("Domain concepts", output.domain_concepts),
        _bullets("Runtime model", output.runtime_model),
        _bullets("Extension points", output.extension_points),
        _bullets("Data model contracts", output.data_model_contracts or output.public_contracts),
        _bullets("Review mental model", output.review_mental_model),
        _bullets("Documentation alignment", output.docs_alignment),
        _bullets("Topology notes", output.top_subsystems + output.dependency_flow),
        _bullets("Risk surfaces", output.risk_surfaces),
        _bullets("Uncertainties", output.uncertainties),
    ]
    summary = "\n".join(p for p in parts if p).strip() or fallback.summary
    return fallback.model_copy(
        update={
            "summary": summary,
            "confidence": "llm_synthesized",
            "tags": sorted(set([*fallback.tags, "repo-summary", "repo-understanding", "contract", "risk-surface"])),
            "metadata": {
                **fallback.metadata,
                "source_record_ids": cited,
                "doc_source_ids": cited_docs,
                "prompt_version": REPO_DISTILL_PROMPT_VERSION,
                "llm_distillation_status": "ok",
                "llm_distillation_warnings": warnings,
                "token_usage": token_usage,
                "omitted_community_summaries": int(omitted.get("community_summaries") or 0),
                "omitted_shard_summaries": int(omitted.get("shard_summaries") or 0),
                "omitted_facts": int(omitted.get("facts") or 0),
            },
        }
    )


def mark_distillation_failed(records: Sequence[ReviewKBRecord], reason: str) -> List[ReviewKBRecord]:
    out: List[ReviewKBRecord] = []
    for record in records:
        meta = dict(record.metadata)
        meta["llm_distillation_status"] = "failed"
        meta["llm_distillation_error"] = reason
        out.append(record.model_copy(update={"metadata": meta}))
    return out


def replace_summary_records(
    existing: Sequence[ReviewKBRecord],
    replacements: Iterable[ReviewKBRecord],
) -> List[ReviewKBRecord]:
    by_id = {r.id: r for r in existing}
    replacement_order: List[str] = []
    for record in replacements:
        if record.id not in by_id:
            replacement_order.append(record.id)
        by_id[record.id] = record
    return [by_id[r.id] for r in existing if r.id in by_id] + [by_id[rid] for rid in replacement_order]


def _bullets(label: str, values: Sequence[str]) -> str:
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if not cleaned:
        return ""
    return f"{label}: " + "; ".join(cleaned[:8])


def _section(label: str, value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    return f"{label}: {cleaned}"
