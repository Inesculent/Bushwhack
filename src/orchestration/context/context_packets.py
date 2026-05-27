"""
Context scoping Phase 0: typed ContextPacket builders per LLM stage.

Authority (highest → lowest):
1. Diff hunks + symbol-local code slices
2. Deterministic tool results (probe, focused fulfill, verifier stdout)
3. Task mandate (paragraph + files + checks)
4. Behavioral spec risks (hypotheses only, not defects)
5. Exploration ledger (capped)

Cleanup has no LLM ContextPacket; Phase 5 adds EvidenceRecord assembly in cleanup.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import (
    BehavioralSpec,
    CandidateFinding,
    FocusedContextResult,
    ReviewTask,
)
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.orchestration.context.mandate_loop_context import (
    bootstrap_digest,
    build_mandate_planning_context,
    format_delta_ledger_for_patch,
    mm_meta,
    spec_excerpt_for_prompt,
)
from src.orchestration.context.review_context import structural_critiquer_context_excerpt
from src.orchestration.nodes.application.worker import ReviewTaskContext
from src.orchestration.review_principles import principles_for_specialty

PACKET_VERSION = "0"

AUTHORITY_NOTES = (
    "Authority: (1) diff/code slices, (2) tool results, (3) task mandate, "
    "(4) behavioral hypotheses (non-defect), (5) exploration ledger."
)

# Stable section key → prompt heading (matches existing reviewer markdown placeholders).
SECTION_HEADINGS: Dict[str, str] = {
    "user_goals": "User goals",
    "docs_prebrief": "Docs prebrief",
    "git_diff_excerpt": "Git diff excerpt",
    "changed_files": "Changed files",
    "intent_summary": "Intent summary",
    "non_goals": "Non-goals",
    "bootstrap_digest": "Bootstrap digest",
    "current_spec_excerpt": "Current spec excerpt",
    "exploration_log": "Exploration log",
    "phase0_inputs": "Phase 0 inputs",
    "behavioral_mandate_excerpt": "Behavioral mandate excerpt",
    "bootstrap_digest_oneliner": "bootstrap_digest_oneliner",
    "draft_tasks_json": "draft_tasks_json",
    "current_tasks_json": "Current tasks JSON",
    "critique_gaps": "Critique gaps",
    "revision_instructions": "Revision instructions",
    "review_principles": "Review principles",
    "code_evidence": "Direct Context Gathered By Tools",
    "structural_excerpt": "Structural context (1-hop)",
    "assigned_task": "Assigned Task",
    "structured_extraction_checklist": "Structured extraction checklist (mandatory)",
    "diff_hunk": "Git Diff Excerpt",
    "mental_model_hypothesis": "Mental model excerpt (optional, pull-based)",
    "exploration_ledger": "Mental model query log (bounded)",
    "reflector_specialty": "Reflector Specialty",
    "candidate_findings_json": "Candidate Findings (JSON lines)",
    "mandate_risks_excerpt": "Behavioral mandate excerpt",
    "behavioral_mandate_excerpt": "behavioral_mandate_excerpt",
    "diff_summary": "Git Diff Excerpt",
    "changed_files_list": "Changed Files",
    "pr_context": "PR context",
    "diff_surface_inventory": "Surfaces introduced in diff",
    "preflight_summary": "Preflight Summary",
    "structural_routing_hints": "Structural Routing Hints",
    "global_insights": "Global Insights",
    "verifier_candidate_json": "candidate_json",
    "verifier_focused_snippets": "focused_context_snippets",
    "verifier_diff_excerpt": "git_diff_excerpt",
    "revision_shard_evidence": "Candidate And Focused Evidence",
    "code_claim_slices": "Code at claim lines",
    "code_evidence": "Repository code evidence",
}


@dataclass(frozen=True)
class ContextSection:
    key: str
    tier: int
    content: str
    source: str = ""


@dataclass
class ContextPacket:
    stage: str
    sections: List[ContextSection]
    char_budget: int
    authority_notes: str = AUTHORITY_NOTES
    metadata: Dict[str, Any] = field(default_factory=dict)


def _section(key: str, tier: int, content: str, *, source: str = "") -> ContextSection:
    return ContextSection(key=key, tier=tier, content=content.strip(), source=source)


# Section keys that must never be fully dropped (truncate in place instead).
_PROTECTED_SECTION_KEYS = frozenset({"code_evidence"})


def _truncate_at_evidence_boundaries(content: str, max_len: int) -> tuple[str, bool]:
    """Trim only at ``--- `` unit boundaries; return (text, byte_chop)."""
    if len(content) <= max_len:
        return content, False
    cut = content[:max_len]
    sep = cut.rfind("\n--- ")
    if sep > max_len // 3:
        return cut[:sep].rstrip() + "\n... [evidence truncated at unit boundary]", False
    return cut.rstrip() + "\n... [truncated]", True


def enforce_packet_budget(packet: ContextPacket) -> ContextPacket:
    """Truncate lowest-tier sections first until total fits char_budget."""
    sections = list(packet.sections)
    chars_by_section: Dict[str, int] = {s.key: len(s.content) for s in sections}
    total = sum(chars_by_section.values())
    dropped: List[str] = []

    if total <= packet.char_budget:
        meta = dict(packet.metadata)
        meta.update(
            {
                "chars_by_section": chars_by_section,
                "dropped_sections": dropped,
                "char_len": total,
                "packet_version": PACKET_VERSION,
                "sections_included": [s.key for s in sections],
            }
        )
        return ContextPacket(
            stage=packet.stage,
            sections=sections,
            char_budget=packet.char_budget,
            authority_notes=packet.authority_notes,
            metadata=meta,
        )

    # Drop from highest tier number (lowest authority) first; skip protected keys.
    by_tier = sorted(sections, key=lambda s: (-s.tier, -len(s.content)))
    remaining = list(sections)
    while total > packet.char_budget and by_tier:
        victim = by_tier.pop(0)
        if victim.key in _PROTECTED_SECTION_KEYS:
            continue
        if victim in remaining:
            remaining.remove(victim)
            dropped.append(victim.key)
            total -= chars_by_section.pop(victim.key, 0)

    # If still over, truncate protected / high-authority sections before dropping them.
    if total > packet.char_budget:
        ordered = sorted(
            remaining,
            key=lambda s: (-s.tier, -len(s.content)),
        )
        for sec in ordered:
            if total <= packet.char_budget:
                break
            idx = next(i for i, s in enumerate(remaining) if s.key == sec.key)
            excess = total - packet.char_budget
            old_len = len(remaining[idx].content)
            if sec.key == "code_evidence":
                new_content, byte_chop = _truncate_at_evidence_boundaries(
                    remaining[idx].content, max(0, old_len - excess)
                )
                meta = dict(packet.metadata)
                meta["byte_chop"] = byte_chop
                packet.metadata = meta
            else:
                new_len = max(0, old_len - excess)
                new_content = remaining[idx].content[:new_len]
                if new_len < old_len:
                    new_content = new_content.rstrip() + "\n... [truncated]"
            remaining[idx] = _section(sec.key, sec.tier, new_content, source=sec.source)
            total -= old_len - len(remaining[idx].content)

    chars_by_section = {s.key: len(s.content) for s in remaining}
    meta = dict(packet.metadata)
    meta.update(
        {
            "chars_by_section": chars_by_section,
            "dropped_sections": dropped,
            "char_len": sum(chars_by_section.values()),
            "packet_version": PACKET_VERSION,
            "sections_included": [s.key for s in remaining],
        }
    )
    return ContextPacket(
        stage=packet.stage,
        sections=remaining,
        char_budget=packet.char_budget,
        authority_notes=packet.authority_notes,
        metadata=meta,
    )


def packet_to_prompt_sections(packet: ContextPacket) -> Dict[str, str]:
    """Map packet sections to render_reviewer_prompt heading → body."""
    out: Dict[str, str] = {}
    for sec in sorted(packet.sections, key=lambda s: s.tier):
        heading = SECTION_HEADINGS.get(sec.key, sec.key.replace("_", " ").title())
        out[heading] = sec.content
    return out


def merge_probe_flags(packet: ContextPacket) -> Dict[str, Any]:
    """Extend critique probe telemetry from packet metadata."""
    meta = packet.metadata
    content = "\n".join(s.content for s in packet.sections)
    flags: Dict[str, Any] = {
        "long_context": meta.get("char_len", len(content)) > 15000,
        "risky_keywords": bool(
            re.search(
                r"(eval\s*\(|exec\s*\(|subprocess|password|secret|token|auth|jwt)",
                content,
                re.IGNORECASE,
            )
        ),
        "char_len": meta.get("char_len", len(content)),
        "packet_version": meta.get("packet_version", PACKET_VERSION),
        "sections_included": meta.get("sections_included", []),
        "chars_by_section": meta.get("chars_by_section", {}),
        "dropped_sections": meta.get("dropped_sections", []),
        "byte_chop": bool(meta.get("byte_chop", False)),
        "files_complete": meta.get("files_complete", {}),
        "symbols_included": meta.get("symbols_included", []),
    }
    return flags


def packet_to_storage_dict(packet: ContextPacket) -> Dict[str, Any]:
    return {
        "stage": packet.stage,
        "char_budget": packet.char_budget,
        "authority_notes": packet.authority_notes,
        "metadata": packet.metadata,
        "sections": [
            {"key": s.key, "tier": s.tier, "content": s.content, "source": s.source}
            for s in packet.sections
        ],
    }


def section_content_from_storage(stored: Dict[str, Any], key: str) -> str:
    for row in stored.get("sections") or []:
        if isinstance(row, dict) and row.get("key") == key:
            return str(row.get("content") or "")
    return ""


def render_packet_plaintext(packet: ContextPacket) -> str:
    """Serialize sections for pipeline direct_context cache (code path)."""
    parts: List[str] = []
    for sec in sorted(packet.sections, key=lambda s: s.tier):
        heading = SECTION_HEADINGS.get(sec.key, sec.key)
        parts.append(f"## {heading}\n{sec.content}")
    return "\n\n".join(parts)


def spec_risks_excerpt_for_prompt(
    ref: str | None,
    settings: Settings | None = None,
) -> str:
    """Risks and uncertainties only — not full BehavioralSpec JSON (plan critic / planner)."""
    settings = settings or get_settings()
    if not ref:
        return "(no behavioral spec yet)"
    try:
        spec = BehavioralSpecStore(settings).read(ref)
        parts = [
            f"Risk hypotheses (non-defects): {spec.risk_hypotheses[:1200]}",
            f"Uncertainties: {spec.uncertainties[:800]}",
        ]
        text = "\n".join(p.strip() for p in parts if p.strip())
        cap = min(int(settings.reviewer_context_plan_critic_max_chars) // 2, 3000)
        return text[:cap] + ("..." if len(text) > cap else "")
    except Exception as exc:  # noqa: BLE001
        return f"(failed to load spec risks: {exc.__class__.__name__})"


def _extract_files_from_diff(git_diff: str) -> List[str]:
    files: List[str] = []
    for line in git_diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null":
                files.append(path)
    return files


def pr_context_from_state(state: GraphState) -> tuple[str, str]:
    meta = dict(state.get("metadata") or {})
    title = str(meta.get("pr_title") or "").strip()
    body = str(meta.get("pr_description") or "").strip()
    return title, body


def classes_introduced_in_diff(git_diff: str) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for line in git_diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = re.match(r"\+\s*class\s+(\w+)", line)
        if not m:
            continue
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def format_pr_context_section(title: str, body: str, *, max_chars: int = 2500) -> str:
    parts: List[str] = []
    if title:
        parts.append(f"Title: {title}")
    if body:
        parts.append(f"Description:\n{body}")
    text = "\n\n".join(parts).strip()
    if not text:
        return ""
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def enrich_intent_summary_with_diff_scope(
    intent_summary: str,
    git_diff: str,
    *,
    min_classes: int = 3,
    mention_ratio: float = 0.6,
) -> tuple[str, List[str]]:
    warnings: List[str] = []
    summary = (intent_summary or "").strip()
    classes = classes_introduced_in_diff(git_diff)
    if len(classes) < min_classes:
        return summary, warnings
    mentioned = sum(1 for c in classes if c in summary)
    threshold = max(1, int(len(classes) * mention_ratio))
    if mentioned >= threshold:
        return summary, warnings
    suffix = f" (Surfaces introduced in diff: {', '.join(classes)}.)"
    warnings.append(f"intent_scope_incomplete:mentioned_{mentioned}_of_{len(classes)}")
    if suffix.strip() in summary:
        return summary, warnings
    return summary.rstrip() + suffix, warnings


def surface_inventory_from_state(state: GraphState) -> List[str]:
    _meta, slot = mm_meta(state)
    stored = slot.get("diff_surface_inventory")
    if isinstance(stored, list) and stored:
        return [str(x) for x in stored if str(x).strip()]
    return classes_introduced_in_diff(state.get("git_diff", "") or "")


def _scope_sections_for_state(
    state: GraphState,
    *,
    pr_max_chars: int = 2500,
    inventory_max_chars: int = 400,
) -> List[ContextSection]:
    title, body = pr_context_from_state(state)
    pr_text = format_pr_context_section(title, body, max_chars=pr_max_chars)
    inventory = surface_inventory_from_state(state)
    inventory_text = ", ".join(inventory) if inventory else ""
    if len(inventory_text) > inventory_max_chars:
        inventory_text = inventory_text[: inventory_max_chars - 3] + "..."
    sections: List[ContextSection] = []
    if pr_text:
        sections.append(_section("pr_context", 1, pr_text, source="metadata"))
    if inventory_text:
        sections.append(
            _section("diff_surface_inventory", 1, inventory_text, source="git_diff")
        )
    return sections


def build_intent_extractor_packet(state: GraphState, *, settings: Settings | None = None) -> ContextPacket:
    settings = settings or get_settings()
    git_diff = state.get("git_diff", "") or ""
    files = _extract_files_from_diff(git_diff)
    char_budget = int(settings.reviewer_context_intent_max_chars)
    title, body = pr_context_from_state(state)
    pr_cap = 2500
    inventory_cap = 400
    pr_text = format_pr_context_section(title, body, max_chars=pr_cap)
    inventory = classes_introduced_in_diff(git_diff)
    inventory_text = ", ".join(inventory) if inventory else ""
    if len(inventory_text) > inventory_cap:
        inventory_text = inventory_text[: inventory_cap - 3] + "..."
    reserved = len(pr_text) + len(inventory_text) + len(", ".join(files[:80])) + 200
    diff_cap = max(1500, char_budget - reserved)

    sections: List[ContextSection] = [
        _section("user_goals", 3, str(state.get("user_goals", "") or ""), source="state"),
        _section("docs_prebrief", 3, str(state.get("docs_prebrief_summary", "") or ""), source="state"),
    ]
    if pr_text:
        sections.append(_section("pr_context", 1, pr_text, source="metadata"))
    if inventory_text:
        sections.append(
            _section("diff_surface_inventory", 1, inventory_text, source="git_diff")
        )
    sections.extend(
        [
            _section("git_diff_excerpt", 1, git_diff[:diff_cap], source="git_diff"),
            _section("changed_files", 1, ", ".join(files[:80]) or "(none)", source="git_diff"),
        ]
    )
    packet = ContextPacket(
        stage="intent_extractor",
        char_budget=char_budget,
        sections=sections,
    )
    return enforce_packet_budget(packet)


def build_mandate_synthesizer_packet(state: GraphState, *, settings: Settings | None = None) -> ContextPacket:
    settings = settings or get_settings()
    _meta, slot = mm_meta(state)
    intent = dict(slot.get("intent_extractor") or {})
    ref = state.get("behavioral_spec_ref")
    ref_str = ref if isinstance(ref, str) else None
    spec_excerpt = spec_excerpt_for_prompt(ref_str, settings) if ref_str else "(no spec yet)"

    sections: List[ContextSection] = [
        _section("intent_summary", 3, str(intent.get("intent_summary", "")), source="mental_model"),
        _section("non_goals", 3, str(intent.get("non_goals", "")), source="mental_model"),
        _section("bootstrap_digest", 3, bootstrap_digest(state) or "(pending)", source="mental_model"),
    ]
    sections.extend(_scope_sections_for_state(state))
    sections.extend(
        [
            _section("current_spec_excerpt", 4, spec_excerpt, source="behavioral_spec"),
            _section(
                "exploration_log",
                5,
                format_delta_ledger_for_patch(state, max_entries=8, max_chars=8000),
                source="exploration_ledger",
            ),
        ]
    )
    packet = ContextPacket(
        stage="mandate_synthesizer",
        char_budget=int(settings.reviewer_context_mandate_synth_max_chars),
        sections=sections,
    )
    return enforce_packet_budget(packet)


def build_plan_critic_packet(
    state: GraphState,
    draft_tasks: Sequence[ReviewTask],
    *,
    settings: Settings | None = None,
    extra_sections: Mapping[str, ContextSection] | None = None,
) -> ContextPacket:
    settings = settings or get_settings()
    _meta, slot = mm_meta(state)
    intent = dict(slot.get("intent_extractor") or {})
    ref = state.get("behavioral_spec_ref")
    ref_str = ref if isinstance(ref, str) else None
    git_diff = state.get("git_diff", "") or ""
    digest_line = (bootstrap_digest(state) or "")[:500]
    _, mm_slot = mm_meta(state)
    bootstrap_completed = bool(mm_slot.get("bootstrap_completed"))
    if bootstrap_completed:
        diff_cap = min(
            2500,
            int(settings.reviewer_context_plan_critic_max_chars) // 2,
        )
    else:
        diff_cap = min(
            4000,
            int(settings.reviewer_context_plan_critic_max_chars) // 2,
        )

    title, body = pr_context_from_state(state)
    pr_short = format_pr_context_section(title, body, max_chars=800)
    inventory = surface_inventory_from_state(state)
    inventory_text = ", ".join(inventory) if inventory else ""
    if len(inventory_text) > 400:
        inventory_text = inventory_text[:397] + "..."

    sections: List[ContextSection] = [
        _section(
            "bootstrap_digest_oneliner",
            3,
            digest_line or "(none)",
            source="mental_model",
        ),
        _section(
            "behavioral_mandate_excerpt",
            4,
            spec_risks_excerpt_for_prompt(ref_str, settings),
            source="behavioral_spec",
        ),
        _section(
            "intent_summary",
            3,
            str(intent.get("intent_summary", ""))[:1500],
            source="mental_model",
        ),
    ]
    if pr_short:
        sections.append(_section("pr_context", 1, pr_short, source="metadata"))
    if inventory_text:
        sections.append(
            _section("diff_surface_inventory", 1, inventory_text, source="git_diff")
        )
    sections.extend(
        [
            _planner_diff_section(
                state,
                git_diff,
                diff_cap,
                bootstrap_completed=bootstrap_completed,
            ),
            _section(
                "draft_tasks_json",
                3,
                json.dumps([t.model_dump() for t in draft_tasks], indent=2)[
                    : max(2000, int(settings.reviewer_context_plan_critic_max_chars) // 2)
                ],
                source="actor_critic_planner",
            ),
        ]
    )
    if extra_sections:
        sections.extend(extra_sections.values())

    packet = ContextPacket(
        stage="plan_critic",
        char_budget=int(settings.reviewer_context_plan_critic_max_chars),
        sections=sections,
    )
    return enforce_packet_budget(packet)


def _normalize_task_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def files_complete_from_pipeline_slot(pipeline_slot: Mapping[str, Any]) -> Dict[str, bool]:
    """Resolve per-file completeness from critique_pipeline by_task slot."""
    out: Dict[str, bool] = {}
    probe = pipeline_slot.get("probe_flags")
    if isinstance(probe, dict):
        fc = probe.get("files_complete")
        if isinstance(fc, dict):
            for k, v in fc.items():
                if isinstance(k, str):
                    out[_normalize_task_path(k)] = bool(v)
    te = pipeline_slot.get("task_evidence")
    if isinstance(te, dict):
        fc = te.get("files_complete")
        if isinstance(fc, dict):
            for k, v in fc.items():
                if isinstance(k, str):
                    out[_normalize_task_path(k)] = bool(v)
    stored = pipeline_slot.get("context_packet")
    if isinstance(stored, dict):
        meta = stored.get("metadata")
        if isinstance(meta, dict):
            fc = meta.get("files_complete")
            if isinstance(fc, dict):
                for k, v in fc.items():
                    if isinstance(k, str):
                        out[_normalize_task_path(k)] = bool(v)
    return out


def task_target_paths(task: ReviewTask) -> List[str]:
    paths = [
        _normalize_task_path(p)
        for p in task.target_files
        if isinstance(p, str) and p.strip()
    ]
    return paths


def diff_hunk_suppressed_for_task(
    task: ReviewTask,
    files_complete: Mapping[str, bool],
) -> bool:
    """True when every task target file is present in code_evidence as complete."""
    targets = task_target_paths(task)
    if not targets:
        return False
    return all(files_complete.get(fp) for fp in targets)


def file_complete_in_task_evidence(
    task_evidence: Mapping[str, Any],
    file_path: str,
) -> bool:
    """True when task evidence marked the file as fully loaded."""
    fc = task_evidence.get("files_complete")
    if not isinstance(fc, dict):
        return False
    norm = _normalize_task_path(file_path)
    return bool(fc.get(norm))


def _planner_diff_section(
    state: GraphState,
    git_diff: str,
    diff_cap: int,
    *,
    bootstrap_completed: bool,
) -> ContextSection:
    """After bootstrap, demote diff so surface inventory drives scope—not a truncated hunk."""
    if bootstrap_completed:
        inventory = surface_inventory_from_state(state)
        from src.orchestration.context.mandate_loop_context import (
            explorer_full_file_read_paths,
        )

        parts = [
            "Diff excerpt omitted after mandate bootstrap. Scope tasks from "
            "'Surfaces introduced in diff' and full repository file bodies (workers load complete files).",
            "Do not limit tasks to classes visible only in a truncated diff hunk.",
        ]
        if inventory:
            parts.append(
                f"Surface inventory ({len(inventory)}): {', '.join(inventory[:24])}"
                + ("..." if len(inventory) > 24 else "")
                + "."
            )
        full_reads = explorer_full_file_read_paths(state)
        if full_reads:
            parts.append(f"Explorer full-file reads: {', '.join(full_reads[:12])}.")
        return _section(
            "diff_summary",
            5,
            " ".join(parts),
            source="git_diff_omitted_post_bootstrap",
        )
    return _section("diff_summary", 1, git_diff[:diff_cap], source="git_diff")


def build_draft_planner_packet(
    state: GraphState,
    *,
    settings: Settings | None = None,
    max_diff_chars: int | None = None,
) -> ContextPacket:
    """Planner prompt sections (mandate context + structural hints + diff)."""
    settings = settings or get_settings()
    from src.orchestration.nodes.application.planner import (
        _structural_routing_hints,
        _target_files,
    )

    files = _target_files(state)
    preflight = state.get("preflight_summary")
    insights = state.get("global_insights", []) or []
    git_diff = state.get("git_diff", "") or ""
    _, mm_slot = mm_meta(state)
    bootstrap_completed = bool(mm_slot.get("bootstrap_completed"))
    if max_diff_chars is not None:
        diff_cap = max_diff_chars
    elif bootstrap_completed:
        diff_cap = min(
            2500,
            int(settings.reviewer_context_plan_critic_max_chars) // 3,
        )
    else:
        diff_cap = int(settings.reviewer_context_plan_critic_max_chars)

    sections: List[ContextSection] = list(
        _scope_sections_for_state(state, pr_max_chars=1500, inventory_max_chars=800)
    )
    sections.extend(
        [
            _section("changed_files_list", 1, str(files), source="planner"),
            _section(
                "preflight_summary",
                2,
                str(preflight.model_dump() if preflight else {}),
                source="state",
            ),
            _section(
                "structural_routing_hints",
                2,
                str(_structural_routing_hints(state, files)),
                source="structural_graph",
            ),
            _section("global_insights", 3, str(insights), source="state"),
            _planner_diff_section(
                state,
                git_diff,
                diff_cap,
                bootstrap_completed=bootstrap_completed,
            ),
        ]
    )
    for key, value in build_mandate_planning_context(state).items():
        norm_key = key.lower().replace(" ", "_")
        if norm_key in {"surfaces_introduced_in_diff", "pr_context"}:
            continue
        sections.append(_section(norm_key, 3, value, source="mandate_loop_context"))

    packet = ContextPacket(
        stage="draft_planner",
        char_budget=int(settings.reviewer_context_plan_critic_max_chars),
        sections=sections,
    )
    return enforce_packet_budget(packet)


def build_plan_revision_packet(
    state: GraphState,
    draft_tasks: Sequence[ReviewTask],
    critique: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> ContextPacket:
    settings = settings or get_settings()
    ref = state.get("behavioral_spec_ref")
    ref_str = ref if isinstance(ref, str) else None
    packet = build_plan_critic_packet(
        state,
        draft_tasks,
        settings=settings,
        extra_sections={
            "critique_gaps": _section(
                "critique_gaps",
                3,
                str(critique.get("gaps", "")),
                source="plan_critic",
            ),
            "revision_instructions": _section(
                "revision_instructions",
                3,
                str(critique.get("revision_instructions", "")),
                source="plan_critic",
            ),
            "current_tasks_json": _section(
                "current_tasks_json",
                3,
                json.dumps([t.model_dump() for t in draft_tasks], indent=2)[
                    : max(2000, int(settings.reviewer_context_plan_critic_max_chars) // 2)
                ],
                source="actor_critic_planner",
            ),
        },
    )
    packet.stage = "plan_revision"
    return packet


def _task_scoped_diff_excerpt(
    state: GraphState,
    task: ReviewTask,
    *,
    max_chars: int,
) -> str:
    from src.orchestration.context.task_evidence import diff_hunk_for_file

    git_diff = state.get("git_diff", "") or ""
    targets = [p for p in task.target_files if isinstance(p, str) and p.strip()]
    if not targets:
        from src.orchestration.context.task_evidence import _extract_files_from_diff

        targets = _extract_files_from_diff(git_diff)[:3]
    per_file = max(500, max_chars // max(1, len(targets)))
    parts: List[str] = []
    for fp in targets[:12]:
        hunk = diff_hunk_for_file(git_diff, fp, max_chars=per_file)
        if hunk.strip():
            parts.append(hunk)
    blob = "\n\n".join(parts)
    if len(blob) > max_chars:
        return blob[: max_chars - 24].rstrip() + "\n... [diff truncated]"
    return blob if blob.strip() else git_diff[:max_chars]


def build_critique_packet(
    state: GraphState,
    task: ReviewTask,
    ctx: ReviewTaskContext,
    *,
    provider: LazyReviewContextProvider | None = None,
    code_evidence: str | None = None,
    evidence_metadata: Mapping[str, Any] | None = None,
    settings: Settings | None = None,
) -> ContextPacket:
    """Task-scoped probe packet: code evidence + specialty principles (tier-separated)."""
    settings = settings or get_settings()
    principles = principles_for_specialty(task.specialty)
    budget = int(settings.reviewer_critique_packet_max_chars)

    if code_evidence is None and provider is not None:
        from src.orchestration.context.task_evidence import build_task_evidence

        bundle = build_task_evidence(state, task, provider, ctx, settings=settings)
        code_evidence = bundle.rendered
        evidence_metadata = bundle.to_storage_dict()

    if code_evidence is None:
        code_evidence = ""

    meta: Dict[str, Any] = {}
    if evidence_metadata:
        meta.update(dict(evidence_metadata))
    fc = meta.get("files_complete") if isinstance(meta.get("files_complete"), dict) else {}
    if diff_hunk_suppressed_for_task(task, fc):
        meta["diff_hunk_suppressed"] = True
        meta["diff_hunk_suppress_reason"] = (
            "Complete file(s) in code_evidence; diff excerpt omitted for critiquer."
        )

    packet = ContextPacket(
        stage="critique_probe",
        char_budget=budget,
        sections=[
            _section("review_principles", 5, principles, source="review_principles"),
            _section("code_evidence", 2, code_evidence, source="task_evidence"),
        ],
        metadata=meta,
    )
    enforced = enforce_packet_budget(packet)
    em = dict(enforced.metadata)
    if evidence_metadata:
        em.setdefault("files_complete", evidence_metadata.get("files_complete", {}))
        em.setdefault("symbols_included", evidence_metadata.get("symbols_included", []))
        if "byte_chop" not in em:
            em["byte_chop"] = evidence_metadata.get("byte_chop", False)
    enforced.metadata = em
    return enforced


def build_critique_probe_packet(
    state: GraphState,
    task: ReviewTask,
    ctx: ReviewTaskContext,
    **kwargs: Any,
) -> ContextPacket:
    """Alias for :func:`build_critique_packet`."""
    return build_critique_packet(state, task, ctx, **kwargs)


def _structured_extraction_critiquer_checklist(task: ReviewTask) -> str:
    """Inject orthogonal structured-result checks when the task mandate targets extraction paths."""
    blob = f"{task.title} {task.description}".lower()
    signals = (
        "structured extraction",
        "structured result",
        "row",
        "tuple",
        "[0]",
        "join(",
        "aggregate",
        "serialize",
        "extract",
    )
    if task.specialty != "logic" or not any(s in blob for s in signals):
        return ""
    return (
        "Before closing this task, audit each in-scope handler for these **distinct** defect families "
        "(emit a separate candidate for each you can support with evidence—do not stop after the first):\n"
        "1) **Slot truncation:** structured rows normalized with only `[0]` when other slots may matter.\n"
        "2) **Boundary / truthiness:** index-0 or whole-record semantics skipped by empty/falsy collections.\n"
        "3) **Aggregation:** `join`, formatters, or serializers receiving absent or non-string elements before return.\n"
        "Stopping after one severe finding in the same handler is a recall failure."
    )


def probe_direct_context_for_task(
    stored_packet: Dict[str, Any],
    *,
    settings: Settings | None = None,
) -> tuple[str, List[str]]:
    """Code-evidence string for critique_pipeline direct_context cache (packet only)."""
    warnings: List[str] = []
    code = section_content_from_storage(stored_packet, "code_evidence")
    if code.strip():
        return code, warnings
    warnings.append("probe_missing_code_evidence")
    return "", warnings


def build_critiquer_packet(
    state: GraphState,
    task: ReviewTask,
    pipeline_slot: Dict[str, Any],
    *,
    exploration_ledger_snippet: str = "",
    settings: Settings | None = None,
) -> ContextPacket:
    settings = settings or get_settings()
    _ = exploration_ledger_snippet  # exploration log not injected into critiquer (mandate uses mental_model)
    stored = pipeline_slot.get("context_packet") if isinstance(pipeline_slot.get("context_packet"), dict) else {}
    code_evidence = (
        section_content_from_storage(stored, "code_evidence")
        or str(pipeline_slot.get("direct_context") or "")
    )
    mental_excerpt = str(pipeline_slot.get("mental_model_excerpt") or "")
    principles = section_content_from_storage(stored, "review_principles") or principles_for_specialty(
        task.specialty
    )

    struct_excerpt = structural_critiquer_context_excerpt(state, task.target_files)
    files_complete = files_complete_from_pipeline_slot(pipeline_slot)
    omit_diff = diff_hunk_suppressed_for_task(task, files_complete)
    diff_cap = int(settings.reviewer_context_critiquer_diff_hunk_max_chars)

    sections: List[ContextSection] = [
        _section(
            "assigned_task",
            3,
            (
                f"Task ID: {task.id}\n"
                f"Task title: {task.title}\n"
                f"Task description: {task.description}\n"
                f"Target files: {task.target_files}"
            ),
            source="task_registry",
        ),
        _section("code_evidence", 2, code_evidence, source="critique_probe"),
    ]
    obligations = pipeline_slot.get("coverage_obligations")
    if isinstance(obligations, list) and obligations:
        lines = []
        for raw in obligations[:20]:
            if not isinstance(raw, dict):
                continue
            lines.append(
                "- {surface} | {dimension} | file_complete={complete} | evidence={evidence}".format(
                    surface=str(raw.get("surface") or ""),
                    dimension=str(raw.get("dimension") or ""),
                    complete=bool(raw.get("files_complete")),
                    evidence=str(raw.get("evidence") or ""),
                )
            )
        if lines:
            sections.append(
                _section(
                    "coverage_obligations",
                    3,
                    "\n".join(lines),
                    source="deterministic_code_shape",
                )
            )
    if struct_excerpt:
        sections.append(
            _section("structural_excerpt", 2, struct_excerpt, source="structural_graph")
        )
    if not omit_diff:
        diff_body = _task_scoped_diff_excerpt(state, task, max_chars=diff_cap)
        sections.append(
            _section("diff_hunk", 1, diff_body, source="git_diff"),
        )
    if mental_excerpt.strip():
        sections.append(
            _section(
                "mental_model_hypothesis",
                4,
                mental_excerpt.strip(),
                source="mental_model_query",
            )
        )
    if principles.strip():
        sections.append(
            _section("review_principles", 5, principles.strip(), source="review_principles"),
        )
    structured_checklist = _structured_extraction_critiquer_checklist(task)
    if structured_checklist:
        sections.append(
            _section(
                "structured_extraction_checklist",
                3,
                structured_checklist,
                source="task_mandate",
            ),
        )

    packet = ContextPacket(
        stage="critiquer",
        char_budget=int(settings.reviewer_critique_packet_max_chars),
        sections=sections,
    )
    enforced = enforce_packet_budget(packet)
    em = dict(enforced.metadata)
    em["files_complete"] = files_complete
    if omit_diff:
        em["diff_hunk_suppressed"] = True
        em["diff_hunk_suppress_reason"] = (
            "Complete file in code_evidence; diff excerpt omitted."
        )
    enforced.metadata = em
    return enforced


def build_reflection_packet(
    state: GraphState,
    specialty: str,
    candidates: Sequence[CandidateFinding],
    *,
    mental_model_ledger_snippet: str = "",
    settings: Settings | None = None,
) -> ContextPacket:
    settings = settings or get_settings()
    lines = [c.model_dump_json() for c in candidates]
    from src.orchestration.context.task_evidence import (
        cited_class_slices_for_candidates,
        claim_slices_for_candidates,
    )

    class_slices = cited_class_slices_for_candidates(state, candidates)
    claim_slices = claim_slices_for_candidates(state, candidates)
    code_body = class_slices.strip()
    if claim_slices.strip():
        code_body = f"{code_body}\n\n{claim_slices}".strip() if code_body else claim_slices.strip()

    sections: List[ContextSection] = [
        _section("reflector_specialty", 3, specialty, source="reflection"),
        _section("candidate_findings_json", 2, "\n".join(lines), source="candidates"),
    ]
    if code_body:
        sections.append(
            _section(
                "code_evidence",
                1,
                code_body,
                source="task_evidence",
            )
        )
    else:
        from src.orchestration.context.task_evidence import (
            diff_hunk_for_file,
            task_evidence_slot_from_state,
        )

        fp = candidates[0].file_path if candidates else ""
        tid = candidates[0].patch_task_id if candidates else ""
        norm_fp = _normalize_task_path(str(fp)) if fp else ""
        te = task_evidence_slot_from_state(state, str(tid)) if tid else {}
        fc = te.get("files_complete") if isinstance(te.get("files_complete"), dict) else {}
        files = te.get("file_contents") if isinstance(te.get("file_contents"), dict) else {}
        file_body = str(files.get(norm_fp) or files.get(fp) or "")
        omit_diff = bool(norm_fp and fc.get(norm_fp) and file_body.strip())
        if omit_diff:
            cap = int(settings.reviewer_context_reflection_max_chars) - 500
            body = file_body if len(file_body) <= cap else file_body[: cap - 24].rstrip() + "\n... [truncated]"
            sections.append(
                _section("code_evidence", 1, body, source="task_evidence"),
            )
        else:
            hunk = ""
            if fp:
                hunk_cap = min(6000, int(settings.reviewer_context_reflection_max_chars) // 2)
                hunk = diff_hunk_for_file(state.get("git_diff", "") or "", fp, max_chars=hunk_cap)
            sections.append(
                _section(
                    "diff_hunk",
                    1,
                    hunk or (state.get("git_diff", "") or "")[:6000],
                    source="git_diff",
                )
            )
    sections.append(
        _section(
            "authority_notes",
            5,
            (
                "File/class excerpts are from the checked-out repository (same tree as the runtime "
                "verifier). A truncated diff excerpt does not mean code is unavailable—cite evidence "
                "from code_evidence sections first."
            ),
            source="review_principles",
        )
    )
    packet = ContextPacket(
        stage=f"reflection_{specialty}",
        char_budget=int(settings.reviewer_context_reflection_max_chars),
        sections=sections,
    )
    if mental_model_ledger_snippet.strip():
        packet.sections.append(
            _section(
                "exploration_ledger",
                5,
                mental_model_ledger_snippet.strip(),
                source="exploration_ledger",
            )
        )
    return enforce_packet_budget(packet)


def _focused_result_snippet_text(res: FocusedContextResult) -> str:
    parts: List[str] = []
    for path, body in (res.file_snippets or {}).items():
        parts.append(f"--- {path} (snippet) ---\n{body[:4000]}")
    for path, body in (res.file_contents_full or {}).items():
        parts.append(f"--- {path} (full) ---\n{body[:4000]}")
    for query, hits in (res.search_hits or {}).items():
        hit_lines = [
            f"{h.file_path}:{h.line_number}: {h.content[:200]}"
            for h in (hits or [])[:10]
        ]
        parts.append(f"Query: {query}\n" + "\n".join(hit_lines))
    if res.warnings:
        parts.append("Warnings: " + "; ".join(res.warnings[:5]))
    return "\n\n".join(parts)


def focused_snippets_for_candidate(
    state: GraphState,
    candidate_id: str,
    *,
    max_chars: int | None = None,
) -> str:
    settings = get_settings()
    if max_chars is None:
        max_chars = int(settings.verifier_focused_context_max_chars)
    chunks: List[str] = []
    for raw in (state.get("focused_context_results", {}) or {}).values():
        if isinstance(raw, FocusedContextResult):
            res = raw
        elif isinstance(raw, dict):
            try:
                res = FocusedContextResult.model_validate(raw)
            except Exception:  # noqa: BLE001
                continue
        else:
            continue
        if res.candidate_id != candidate_id:
            continue
        chunks.append(_focused_result_snippet_text(res))
    blob = "\n\n".join(chunks)
    if len(blob) > max_chars:
        return blob[:max_chars] + "\n... [truncated]"
    return blob


def build_verifier_generator_packet(
    state: GraphState,
    candidate: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> ContextPacket:
    settings = settings or get_settings()
    from src.orchestration.context.task_evidence import (
        code_slice_from_task_evidence,
        task_evidence_slot_from_state,
    )

    cid = str(candidate.get("candidate_id", ""))
    keep = (
        "candidate_id",
        "file_path",
        "line_start",
        "line_end",
        "claim_type",
        "content",
        "failure_mode",
        "evidence_summary",
        "required_context",
        "confidence",
        "severity",
    )
    slim = {k: candidate[k] for k in keep if k in candidate}
    focused = focused_snippets_for_candidate(state, cid)
    tid = str(candidate.get("patch_task_id", "") or "")
    if tid:
        te = task_evidence_slot_from_state(state, tid)
        claim = code_slice_from_task_evidence(
            te,
            str(candidate.get("file_path", "") or ""),
            int(candidate.get("line_start", 1) or 1),
            int(candidate.get("line_end", 1) or 1),
        )
        if claim.strip():
            focused = f"{focused}\n\n### Task evidence\n{claim}" if focused else claim
    diff_cap = min(2000, int(settings.reviewer_context_verifier_gen_max_chars) // 4)

    packet = ContextPacket(
        stage="verifier_generator",
        char_budget=int(settings.reviewer_context_verifier_gen_max_chars),
        sections=[
            _section(
                "verifier_candidate_json",
                2,
                json.dumps(slim, indent=2, ensure_ascii=False),
                source="candidate",
            ),
            _section(
                "verifier_focused_snippets",
                2,
                focused or "(none)",
                source="focused_context_results",
            ),
            _section(
                "verifier_diff_excerpt",
                1,
                (state.get("git_diff", "") or "")[:diff_cap],
                source="git_diff",
            ),
        ],
    )
    return enforce_packet_budget(packet)


def build_critique_revision_shard_packet(
    state: GraphState,
    *,
    candidate: CandidateFinding,
    focused_results: Sequence[FocusedContextResult],
    verifier_advisory: str = "",
    settings: Settings | None = None,
) -> ContextPacket:
    """Shard digest prompt — never reads critique_pipeline direct_context."""
    settings = settings or get_settings()
    max_cand = int(settings.reviewer_critique_revision_max_candidate_chars)
    cand_raw = candidate.model_dump_json()
    if len(cand_raw) > max_cand:
        cand_raw = cand_raw[:max_cand] + "\n... [truncated]"

    from src.orchestration.context.task_evidence import (
        cited_class_slices_for_candidates,
        code_slice_from_task_evidence,
        diff_hunk_for_file,
        task_evidence_slot_from_state,
    )

    focused_parts: List[str] = [f"### Candidate ({candidate.candidate_id})\n{cand_raw}"]
    te = task_evidence_slot_from_state(state, candidate.patch_task_id)
    class_slice = cited_class_slices_for_candidates(state, [candidate])
    if class_slice.strip():
        focused_parts.append(class_slice)
    claim_slice = code_slice_from_task_evidence(
        te,
        candidate.file_path,
        candidate.line_start,
        candidate.line_end,
    )
    if claim_slice.strip():
        focused_parts.append(f"#### Task evidence at claim lines\n{claim_slice}")

    max_shard = int(settings.reviewer_critique_revision_max_shard_chars)
    used = sum(len(p) for p in focused_parts)
    for res in focused_results:
        snippet = _focused_result_snippet_text(res)
        block = f"#### Focused context {res.request_id}\n{snippet}"
        if used + len(block) > max_shard:
            block = block[: max(0, max_shard - used)] + "\n... [truncated]"
        focused_parts.append(block)
        used += len(block)
        if used >= max_shard:
            break

    body = "\n\n".join(focused_parts)
    sections: List[ContextSection] = [
        _section("revision_shard_evidence", 2, body, source="focused_results"),
    ]
    if verifier_advisory.strip():
        sections.append(
            _section("verifier_advisory", 2, verifier_advisory.strip(), source="verifier_reports")
        )

    has_code_evidence = bool(
        class_slice.strip() or claim_slice.strip() or file_complete_in_task_evidence(te, candidate.file_path)
    )
    omit_diff = file_complete_in_task_evidence(te, candidate.file_path) and has_code_evidence
    if not omit_diff:
        diff_cap = int(settings.reviewer_context_critiquer_diff_hunk_max_chars)
        diff_body = diff_hunk_for_file(
            state.get("git_diff", "") or "",
            candidate.file_path,
            max_chars=diff_cap,
        )
        if not diff_body.strip():
            diff_body = (state.get("git_diff", "") or "")[:diff_cap]
        sections.append(
            _section(
                "diff_hunk",
                6 if has_code_evidence else 1,
                diff_body,
                source="git_diff",
            )
        )

    packet = ContextPacket(
        stage="critique_revision_shard",
        char_budget=max_shard + max_cand,
        sections=sections,
        metadata={"files_complete": dict(te.get("files_complete") or {})},
    )
    enforced = enforce_packet_budget(packet)
    em = dict(enforced.metadata)
    if omit_diff:
        em["diff_hunk_suppressed"] = True
        em["diff_hunk_suppress_reason"] = (
            "Complete file in task evidence; diff excerpt omitted for revision shard."
        )
    enforced.metadata = em
    return enforced
