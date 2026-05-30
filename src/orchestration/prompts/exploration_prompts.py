"""Prompt builders for LLM-backed exploration nodes (templates live under prompts/exploration/)."""

from __future__ import annotations

from typing import Sequence

from src.domain.schemas import CommunitySemanticSummary, CommunityWorkItem, ReviewKBRecord

from src.orchestration.prompts.renderer import load_exploration_prompt


def _fill_template(template: str, **values: str) -> str:
    """Substitute `{key}` placeholders; safe when values contain ``{`` or ``}``."""
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    return result


def render_explorer_prompt(*, repo_path: str, user_goals: str, git_diff: str) -> str:
    """Prompt the first-pass explorer to build an initial understanding map from the diff."""
    diff_excerpt = (git_diff or "")[:12000] or "(empty diff)"
    return _fill_template(
        load_exploration_prompt("explorer.md"),
        repo_path=repo_path or "(unknown)",
        user_goals=user_goals or "(none provided)",
        git_diff=diff_excerpt,
    )


def render_docs_prebrief_prompt(
    *,
    repo_path: str,
    docs: str,
    pr_context: str,
    issues: str,
    comments: str,
) -> str:
    """Prompt a docs pre-brief summary from README/CONTRIBUTING/PR context."""
    return _fill_template(
        load_exploration_prompt("docs_prebrief.md"),
        repo_path=repo_path or "(unknown)",
        docs=docs or "(none)",
        pr_context=pr_context or "(none)",
        issues=issues or "(none)",
        comments=comments or "(none)",
    )


def render_community_semantic_prompt(*, repo_path: str, item: CommunityWorkItem) -> str:
    """Prompt a community agent to summarize observable structure and responsibilities."""
    files = "\n".join(f"- {p}" for p in item.file_paths[:200])
    symbols = "\n\n".join(item.symbol_context_lines[:500])
    outbound = ", ".join(item.outbound_cross_community_targets[:200])
    return _fill_template(
        load_exploration_prompt("community_semantic.md"),
        repo_path=repo_path or "(unknown)",
        community_id=str(item.community_id),
        outbound=outbound or "(none)",
        files=files or "(none)",
        symbols=symbols or "(none)",
    )


def render_unverified_call_resolver_prompt(*, symbol_node_id: str, body_text: str) -> str:
    """Prompt the resolver to describe a symbol's observable behavior from its body."""
    body_excerpt = (body_text or "")[:6000]
    return _fill_template(
        load_exploration_prompt("unverified_call_resolver.md"),
        symbol_node_id=symbol_node_id,
        body_text=body_excerpt,
    )


def render_semantic_merge_prompt(summaries: Sequence[CommunitySemanticSummary]) -> str:
    """Prompt the merge node to synthesize a repository-level understanding map."""
    lines = [
        f"- community {s.community_id}: {s.label} - {s.purpose} "
        f"(deps: {', '.join(str(x) for x in s.cross_community_dependencies) or 'none'})"
        for s in sorted(summaries, key=lambda s: s.community_id)
    ]
    block = "\n".join(lines[:400])
    return _fill_template(
        load_exploration_prompt("semantic_merge.md"),
        community_summaries=block,
    )


def render_repository_kb_community_distill_prompt(*, pack_json: str) -> str:
    """Prompt bounded community-level Repository KB distillation."""
    return _fill_template(
        load_exploration_prompt("repository_kb_community_distill.md"),
        pack_json=pack_json,
    )


def render_repository_kb_shard_distill_prompt(*, pack_json: str) -> str:
    """Prompt bounded lane/shard Repository KB distillation."""
    return _fill_template(
        load_exploration_prompt("repository_kb_shard_distill.md"),
        pack_json=pack_json,
    )


def render_repository_kb_community_merge_prompt(*, pack_json: str) -> str:
    """Prompt community-level synthesis from shard summary records."""
    return _fill_template(
        load_exploration_prompt("repository_kb_community_merge.md"),
        pack_json=pack_json,
    )


def render_repository_kb_repo_distill_prompt(*, pack_json: str) -> str:
    """Prompt bounded repo-level Repository KB distillation."""
    return _fill_template(
        load_exploration_prompt("repository_kb_repo_distill.md"),
        pack_json=pack_json,
    )


def render_semantic_merge_from_kb_prompt(
    summaries: Sequence[ReviewKBRecord],
    *,
    max_chars: int | None = None,
) -> str:
    """Prompt global synthesis from Repository KB summary records."""
    lines = []
    budget = max_chars or 120000
    for record in summaries:
        source_ids = ", ".join(str(x) for x in record.metadata.get("source_record_ids") or [])
        line = (
            f"- {record.id} ({record.confidence}; sources: {source_ids or 'none'}): "
            f"{record.summary[:500]}"
        )
        if len("\n".join([*lines, line])) > budget:
            break
        lines.append(line)
    return _fill_template(
        load_exploration_prompt("semantic_merge.md"),
        community_summaries="\n".join(lines),
    )
