"""Context budgeting for coupled mandate-plan loop (ledger deltas, digests, spec excerpts)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import BehavioralSpec
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.orchestration.prompts.ledger_formatter import format_exploration_ledger_for_prompt


def mm_meta(state: GraphState) -> tuple[Dict[str, Any], Dict[str, Any]]:
    meta = dict(state.get("metadata", {}) or {})
    slot = dict(meta.get("mental_model", {}) or {})
    return meta, slot


def _mm_slot(state: GraphState) -> Dict[str, Any]:
    return mm_meta(state)[1]


def patch_seq(state: GraphState) -> int:
    return int(_mm_slot(state).get("patch_seq", 0))


def ledger_applied_count(state: GraphState) -> int:
    """Number of exploration_ledger entries already consumed by mandate_patch."""
    return int(_mm_slot(state).get("ledger_applied_count", 0))


def bootstrap_digest(state: GraphState) -> str:
    return str(_mm_slot(state).get("bootstrap_digest", "") or "")


def explorer_mode(state: GraphState) -> str:
    slot = _mm_slot(state)
    return str(slot.get("explorer_mode") or "bootstrap")


def coupled_loop_meta(state: GraphState) -> Dict[str, Any]:
    return dict(_mm_slot(state).get("coupled_loop", {}) or {})


def ledger_since_last_patch(state: GraphState) -> List[Dict[str, Any]]:
    """Return mandate_tool_observation entries not yet applied to a patch."""
    start = ledger_applied_count(state)
    ledger = state.get("exploration_ledger") or []
    out: List[Dict[str, Any]] = []
    for i, entry in enumerate(ledger):
        if i < start:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "mandate_tool_observation":
            continue
        out.append(entry)
    return out


def format_delta_ledger_for_patch(
    state: GraphState,
    *,
    max_entries: int = 12,
    max_chars: int = 12000,
) -> str:
    entries = ledger_since_last_patch(state)
    if not entries:
        return "(no new tool observations)"
    text, _stats = format_exploration_ledger_for_prompt(
        entries,
        max_entries=max_entries,
        max_chars=max_chars,
        header="New exploration observations (delta)",
    )
    return text


_REPOSITORY_CONTRACT_TERMS = (
    "api",
    "caller",
    "contract",
    "declare",
    "expect",
    "input",
    "invariant",
    "mode",
    "option",
    "output",
    "return",
    "schema",
    "serialize",
    "state",
    "type",
)


def _squash_context_line(text: str, *, max_chars: int) -> str:
    squashed = " ".join((text or "").split())
    if len(squashed) <= max_chars:
        return squashed
    return squashed[: max_chars - 3].rstrip() + "..."


def _record_value(raw: Any, key: str) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(key)
    return getattr(raw, key, None)


def _changed_context_terms(state: GraphState) -> set[str]:
    from src.orchestration.nodes.application.planner import _target_files

    terms: set[str] = set()
    for path in _target_files(state)[:12]:
        norm = str(path or "").replace("\\", "/").strip("/")
        if norm:
            terms.add(norm.lower())
            terms.add(norm.rsplit("/", 1)[-1].lower())
    _, slot = mm_meta(state)
    inventory = slot.get("diff_surface_inventory")
    if isinstance(inventory, list):
        for item in inventory[:20]:
            text = str(item or "").strip().lower()
            if text:
                terms.add(text)
                terms.add(text.rsplit(".", 1)[-1])
    return {term for term in terms if len(term) >= 3}


def _repo_context_line_relevant(line: str, changed_terms: set[str]) -> bool:
    lowered = line.lower()
    if any(term in lowered for term in changed_terms):
        return True
    return any(term in lowered for term in _REPOSITORY_CONTRACT_TERMS)


def build_repository_contract_context(
    state: GraphState,
    *,
    max_chars: int = 1200,
) -> str:
    """Small repo-memory slice for mandate prompts; excludes raw KB bodies."""

    changed_terms = _changed_context_terms(state)
    lines: List[str] = []

    global_summary = str(state.get("global_summary") or "").strip()
    if global_summary:
        selected = [
            _squash_context_line(line, max_chars=260)
            for line in global_summary.splitlines()
            if _repo_context_line_relevant(line, changed_terms)
        ][:3]
        if selected:
            lines.append("Repo summary:")
            lines.extend(f"- {line}" for line in selected)

    records = state.get("repository_kb_summary_records") or []
    record_lines: List[str] = []
    for raw in records:
        kind = str(_record_value(raw, "kind") or "")
        if kind not in {"summary", "file", "symbol"}:
            continue
        summary = str(_record_value(raw, "summary") or "").strip()
        if not summary or not _repo_context_line_relevant(summary, changed_terms):
            continue
        rid = str(_record_value(raw, "id") or kind).strip()
        record_lines.append(f"- {rid}: {_squash_context_line(summary, max_chars=240)}")
        if len(record_lines) >= 4:
            break
    if record_lines:
        lines.append("Repository KB summaries:")
        lines.extend(record_lines)

    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def explorer_full_file_read_paths(state: GraphState) -> List[str]:
    """Paths read via mandate explorer read_file with full_file=True."""
    paths: List[str] = []
    seen: set[str] = set()
    for entry in state.get("exploration_ledger") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "mandate_tool_observation":
            continue
        if str(entry.get("tool") or "") != "read_file":
            continue
        args_preview = str(entry.get("args_preview") or "")
        if "full_file" not in args_preview:
            continue
        m = re.search(r"file_path['\"]?\s*[:=]\s*['\"]?([^'\"]+)", args_preview)
        if not m:
            continue
        norm = m.group(1).strip().replace("\\", "/").lstrip("/")
        if norm and norm not in seen:
            seen.add(norm)
            paths.append(norm)
    return paths[:48]


def mandate_explorer_git_diff_cap(
    state: GraphState,
    *,
    default: int = 10_000,
    reduced: int = 4_000,
) -> int:
    """Shrink diff excerpt after explorer has read target files in full."""
    from src.orchestration.nodes.application.planner import _target_files

    targets = [
        p.strip().replace("\\", "/").lstrip("/")
        for p in _target_files(state)
        if isinstance(p, str) and p.strip()
    ]
    if not targets:
        return default
    full_reads = set(explorer_full_file_read_paths(state))
    observed = set(already_observed_paths(state))
    norm_targets = set(targets)
    if full_reads & norm_targets:
        return reduced
    if norm_targets and norm_targets <= observed:
        return reduced
    _, slot = mm_meta(state)
    if slot.get("bootstrap_completed") and observed & norm_targets:
        return reduced
    return default


def already_observed_paths(state: GraphState) -> List[str]:
    paths: List[str] = []
    seen: set[str] = set()
    digest = bootstrap_digest(state)
    for token in re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|go|rs|java)\b", digest):
        norm = token.strip().replace("\\", "/")
        if norm and norm not in seen:
            seen.add(norm)
            paths.append(norm)
    for entry in state.get("exploration_ledger") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "mandate_tool_observation":
            continue
        tool = str(entry.get("tool") or "")
        args_preview = str(entry.get("args_preview") or "")
        if tool == "read_file" and "file_path" in args_preview:
            m = re.search(r"file_path['\"]?\s*[:=]\s*['\"]?([^'\"]+)", args_preview)
            if m:
                norm = m.group(1).strip().replace("\\", "/")
                if norm and norm not in seen:
                    seen.add(norm)
                    paths.append(norm)
    return paths[:48]


def spec_excerpt_for_prompt(
    ref: str | None,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    if not ref:
        return "(no behavioral spec yet)"
    try:
        spec = BehavioralSpecStore(settings).read(ref)
        blob = spec.model_dump_json(indent=2)
        cap = int(settings.reviewer_mandate_spec_excerpt_max_chars)
        return blob[:cap] + ("..." if len(blob) > cap else "")
    except Exception as exc:  # noqa: BLE001
        return f"(failed to load spec: {exc.__class__.__name__})"


def _truncate_digest_field(text: str, max_len: int) -> str:
    chunk = (text or "").strip()
    if len(chunk) <= max_len:
        return chunk
    cut = chunk[:max_len]
    for sep in (". ", "; ", "| "):
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[: idx + len(sep.rstrip())].rstrip()
    return cut.rstrip() + "..."


def build_bootstrap_digest(
    spec: BehavioralSpec,
    *,
    max_chars: int = 1200,
    surface_inventory: Sequence[str] | None = None,
) -> str:
    surfaces = ", ".join(surface_inventory) if surface_inventory else ""
    if not surfaces and "Surfaces introduced in diff:" in (spec.intent_summary or ""):
        m = re.search(r"Surfaces introduced in diff:\s*([^.)]+)", spec.intent_summary)
        if m:
            surfaces = m.group(1).strip()
    parts = [
        f"Intent: {_truncate_digest_field(spec.intent_summary, 200)}",
    ]
    if surfaces:
        parts.append(f"Surfaces: {_truncate_digest_field(surfaces, 300)}")
    parts.append(f"Contracts: {_truncate_digest_field(spec.contract_boundaries, 600)}")
    if spec.contract_questions:
        question_digest = "; ".join(
            f"{q.owner}:{q.dimension}:{q.breach_question}"
            for q in spec.contract_questions[:5]
        )
        parts.append(f"Questions: {_truncate_digest_field(question_digest, 450)}")
    parts.append(f"Risks: {_truncate_digest_field(spec.risk_hypotheses, 450)}")
    for ref in spec.evidence_refs[:6]:
        parts.append(f"File: {ref.ref}")
    text = " | ".join(p.strip() for p in parts if p.strip())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text or "(empty mandate digest)"


def enforce_ledger_cap(state: GraphState, settings: Settings | None = None) -> List[Dict[str, Any]]:
    """Return ledger entries to keep (drop oldest mandate observations when over cap)."""
    settings = settings or get_settings()
    cap = int(settings.reviewer_mandate_ledger_max_total_chars)
    ledger = [e for e in (state.get("exploration_ledger") or []) if isinstance(e, dict)]
    total = sum(len(str(e.get("result_preview") or "")) for e in ledger)
    if total <= cap:
        return []
    mandate_idxs = [
        i
        for i, e in enumerate(ledger)
        if e.get("kind") == "mandate_tool_observation"
    ]
    drops: List[Dict[str, Any]] = []
    while total > cap and mandate_idxs:
        idx = mandate_idxs.pop(0)
        entry = ledger[idx]
        total -= len(str(entry.get("result_preview") or ""))
        drops.append({"drop_ledger_index": idx, "dedupe_key": entry.get("dedupe_key")})
    return drops


def build_mandate_planning_context(state: GraphState) -> Dict[str, str]:
    """Sections for draft_planner / joint critic prompts."""
    settings = get_settings()
    ref = state.get("behavioral_spec_ref")
    ref_str = ref if isinstance(ref, str) else None
    slot = _mm_slot(state)
    intent = dict(slot.get("intent_extractor") or {})
    from src.tools.mandate_exploration_tools import _community_digest_for_changed_files
    from src.orchestration.context.context_packets import (
        classes_introduced_in_diff,
        format_pr_context_section,
        pr_context_from_state,
    )
    from src.orchestration.nodes.application.planner import _target_files

    title, body = pr_context_from_state(state)
    pr_text = format_pr_context_section(title, body, max_chars=2000)
    inventory = slot.get("diff_surface_inventory")
    if isinstance(inventory, list) and inventory:
        surface_line = ", ".join(str(x) for x in inventory if str(x).strip())
    else:
        surface_line = ", ".join(classes_introduced_in_diff(state.get("git_diff", "") or ""))

    ctx: Dict[str, str] = {
        "Intent summary": str(intent.get("intent_summary", "")),
        "Bootstrap digest": bootstrap_digest(state) or "(pending)",
        "Behavioral mandate excerpt": spec_excerpt_for_prompt(ref_str, settings),
        "Community digest": _community_digest_for_changed_files(
            state,
            _target_files(state),
            max_chars=int(settings.reviewer_mandate_bootstrap_digest_max_chars) * 4,
        ),
    }
    if pr_text:
        ctx["PR context"] = pr_text
    if surface_line:
        ctx["Surfaces introduced in diff"] = surface_line
    return ctx


def should_skip_bootstrap_explorer(state: GraphState) -> bool:
    """Skip bootstrap when snapshot resume already has spec + digest."""
    if state.get("behavioral_spec_ref") and bootstrap_digest(state):
        return True
    slot = _mm_slot(state)
    if slot.get("bootstrap_completed"):
        return True
    return False
