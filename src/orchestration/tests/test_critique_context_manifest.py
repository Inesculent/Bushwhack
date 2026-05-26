"""Golden-style manifest: critique packet section keys and tiers."""

from __future__ import annotations

from src.domain.schemas import ReviewTask
from src.domain.state import GraphState
from src.orchestration.context.context_packets import (
    build_critique_packet,
    build_critiquer_packet,
    packet_to_storage_dict,
)
from src.orchestration.nodes.application.worker import ReviewTaskContext


def test_critique_packet_manifest_keys() -> None:
    task = ReviewTask(
        id="t1",
        title="StringCompare",
        description="Review StringCompare execute",
        target_files=["nodes_string.py"],
        specialty="logic",
    )
    state: GraphState = {
        "run_id": "t",
        "repo_path": "/repo",
        "git_diff": "diff --git a/nodes_string.py b/nodes_string.py\n+++ b/nodes_string.py\n@@\n+pass\n",
        "user_goals": "",
        "metadata": {},
    }
    ctx = ReviewTaskContext(file_snippets={"nodes_string.py": "def execute(): pass"})
    probe = build_critique_packet(state, task, ctx, code_evidence="--- nodes_string.py ---\ncode")
    probe_keys = sorted(s.key for s in probe.sections)
    assert probe_keys == ["code_evidence", "review_principles"]

    slot = {
        "direct_context": "code",
        "mental_model_excerpt": "- hypothesis: check encoding",
        "context_packet": packet_to_storage_dict(probe),
    }
    crit = build_critiquer_packet(state, task, slot)
    manifest = [(s.key, s.tier) for s in sorted(crit.sections, key=lambda x: x.tier)]
    assert ("code_evidence", 2) in manifest
    assert ("diff_hunk", 1) in manifest
    assert ("mental_model_hypothesis", 4) in manifest
    assert ("assigned_task", 3) in manifest
    assert all(k != "exploration_ledger" for k, _ in manifest)
