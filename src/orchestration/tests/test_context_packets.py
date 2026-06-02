"""Context scoping Phase 0: ContextPacket builders and budget enforcement."""

from __future__ import annotations

from src.domain.schemas import (
    BehavioralSpec,
    CandidateFinding,
    FocusedContextResult,
    ReviewTask,
    SearchResult,
)
from src.domain.state import GraphState
from src.orchestration.context.context_packets import (
    AUTHORITY_NOTES,
    ContextPacket,
    ContextSection,
    build_critique_probe_packet,
    build_critiquer_packet,
    build_critique_revision_shard_packet,
    build_draft_planner_packet,
    build_intent_extractor_packet,
    build_plan_critic_packet,
    build_reflection_packet,
    build_verifier_generator_packet,
    classes_introduced_in_diff,
    enrich_intent_summary_with_diff_scope,
    enforce_packet_budget,
    focused_snippets_for_candidate,
    merge_probe_flags,
    packet_to_storage_dict,
    spec_risks_excerpt_for_prompt,
)
from src.orchestration.nodes.application.worker import ReviewTaskContext


def _minimal_state(**overrides: object) -> GraphState:
    base: GraphState = {
        "run_id": "test-run",
        "repo_path": "/tmp/repo",
        "git_diff": "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n@@\n+pass\n",
        "user_goals": "Fix unicode handling",
        "metadata": {},
    }
    base.update(overrides)  # type: ignore[typeddict-unknown-key]
    return base


def test_packet_budget_truncates_lowest_tier_first() -> None:
    packet = ContextPacket(
        stage="test",
        char_budget=50,
        sections=[
            ContextSection(key="diff_hunk", tier=1, content="A" * 40, source="git_diff"),
            ContextSection(key="exploration_ledger", tier=5, content="B" * 40, source="ledger"),
        ],
        authority_notes=AUTHORITY_NOTES,
    )
    enforced = enforce_packet_budget(packet)
    keys = {s.key for s in enforced.sections}
    assert "diff_hunk" in keys
    assert "exploration_ledger" not in keys
    assert enforced.metadata.get("dropped_sections") == ["exploration_ledger"]


def test_focused_snippets_render_file_body_before_search_hits_and_kb() -> None:
    state: GraphState = {
        "run_id": "r1",
        "repo_path": "/tmp/repo",
        "metadata": {},
        "focused_context_results": {
            "r1": FocusedContextResult(
                request_id="r1",
                candidate_id="check-1",
                file_snippets={
                    "repository_kb_context": "KB class declaration only",
                    "pkg/mod.py": "class Target:\n    def execute(self):\n        return False\n",
                },
                search_hits={
                    "Target": [
                        SearchResult(
                            file_path="pkg/mod.py",
                            line_number=1,
                            content="class Target:",
                            context_lines=[],
                        )
                    ]
                },
            )
        },
    }

    text = focused_snippets_for_candidate(state, "check-1")

    assert text.index("--- pkg/mod.py (snippet) ---") < text.index("Query: Target")
    assert text.index("Query: Target") < text.index("--- repository_kb_context (snippet) ---")


def test_critiquer_packet_authority_separation() -> None:
    task = ReviewTask(
        id="t1",
        title="Unicode",
        description="Check encoding",
        target_files=["foo.py"],
    )
    state = _minimal_state()
    slot = {
        "direct_context": "file excerpt content",
        "mental_model_excerpt": "hypothesis: maybe wrong codec",
        "context_packet": packet_to_storage_dict(
            build_critique_probe_packet(
                state,
                task,
                ReviewTaskContext(file_snippets={"foo.py": "x = 1"}),
            )
        ),
    }
    packet = build_critiquer_packet(
        state,
        task,
        slot,
        exploration_ledger_snippet="ledger row",
    )
    keys = {s.key for s in packet.sections}
    assert "code_evidence" in keys
    assert "mental_model_hypothesis" in keys
    assert "exploration_ledger" not in keys
    code = next(s for s in packet.sections if s.key == "code_evidence")
    assert "hypothesis" not in code.content.lower() or "codec" not in code.content
    mm = next(s for s in packet.sections if s.key == "mental_model_hypothesis")
    assert "hypothesis" in mm.content


def test_critique_probe_keeps_code_drops_principles_when_over_budget() -> None:
    task = ReviewTask(
        id="t1",
        title="T",
        description="D",
        target_files=["foo.py"],
    )
    state = _minimal_state()
    huge_code = "X" * 30_000
    ctx = ReviewTaskContext(file_snippets={"foo.py": huge_code})
    packet = build_critique_probe_packet(
        state,
        task,
        ctx,
        code_evidence=huge_code,
    )
    keys = {s.key for s in packet.sections}
    assert "code_evidence" in keys
    assert "review_principles" not in keys
    code = next(s for s in packet.sections if s.key == "code_evidence")
    assert len(code.content) > 1000
    assert "code_evidence" not in (packet.metadata.get("dropped_sections") or [])


def test_probe_direct_context_missing_code_evidence() -> None:
    stored = {
        "sections": [{"key": "review_principles", "tier": 5, "content": "p", "source": ""}],
        "metadata": {"dropped_sections": ["code_evidence"]},
    }
    from src.orchestration.context.context_packets import probe_direct_context_for_task

    text, warnings = probe_direct_context_for_task(stored)
    assert text == ""
    assert "probe_missing_code_evidence" in warnings


def test_planner_packet_shrinks_diff_after_bootstrap() -> None:
    huge_diff = "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n" + ("+line\n" * 5000)
    state = _minimal_state(
        git_diff=huge_diff,
        metadata={
            "mental_model": {
                "bootstrap_completed": True,
                "diff_surface_inventory": ["Alpha", "Beta"],
            }
        },
    )
    packet = build_draft_planner_packet(state)
    diff_sec = next(s for s in packet.sections if s.key == "diff_summary")
    assert diff_sec.tier >= 5
    assert "omitted" in diff_sec.content.lower()
    assert "Alpha" in diff_sec.content
    inv = next(s for s in packet.sections if s.key == "diff_surface_inventory")
    assert inv.tier == 1


def test_critiquer_packet_keeps_diff_when_files_complete() -> None:
    task = ReviewTask(
        id="t1",
        title="String nodes",
        description="Review execute methods",
        target_files=["comfy_extras/nodes_string.py"],
    )
    body = "def execute():\n    return None\n"
    state = _minimal_state()
    slot = {
        "direct_context": body,
        "context_packet": {"sections": []},
        "probe_flags": {"files_complete": {"comfy_extras/nodes_string.py": True}},
        "task_evidence": {
            "files_complete": {"comfy_extras/nodes_string.py": True},
            "file_contents": {"comfy_extras/nodes_string.py": body},
        },
    }
    packet = build_critiquer_packet(state, task, slot)
    keys = {s.key for s in packet.sections}
    assert "diff_hunk" in keys
    assert packet.metadata.get("diff_hunk_included_with_complete_evidence") is True
    code = next(s for s in packet.sections if s.key == "code_evidence")
    assert "execute" in code.content


def test_critiquer_packet_task_scoped_diff() -> None:
    task = ReviewTask(
        id="t1",
        title="T",
        description="D",
        target_files=["foo.py"],
    )
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+x = 1\n"
        "diff --git a/bar.py b/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+y = 2\n"
    )
    state = _minimal_state(git_diff=diff)
    slot = {"direct_context": "code", "context_packet": {"sections": []}}
    packet = build_critiquer_packet(state, task, slot)
    hunk = next(s for s in packet.sections if s.key == "diff_hunk")
    assert "foo.py" in hunk.content
    assert "bar.py" not in hunk.content


def test_critiquer_packet_includes_selected_contract_lens_cards() -> None:
    task = ReviewTask(
        id="t1",
        title="Serialize records",
        description="Review JSON output shape for record fields",
        target_files=["foo.py"],
    )
    slot = {
        "direct_context": "def emit(records):\n    return json.dumps([row['id'] for row in records])\n",
        "context_packet": {"sections": []},
        "coverage_obligations": [
            {
                "surface": "emit",
                "dimension": "representation fidelity",
                "evidence": "schema names records with id and name fields",
            }
        ],
    }

    packet = build_critiquer_packet(_minimal_state(), task, slot)

    cards = next(s for s in packet.sections if s.key == "contract_lens_cards")
    assert "Representation fidelity" in cards.content
    assert "Shape/cardinality" in cards.content
    assert cards.content.count("### ") <= 4
    assert "Ownership/lifecycle" not in cards.content


def test_principles_for_specialty_logic_in_probe() -> None:
    from src.orchestration.review_principles import (
        IN_FUNCTION_CONTRACT_GUIDANCE,
        principles_for_specialty,
    )

    task = ReviewTask(
        id="t1",
        title="T",
        description="D",
        target_files=["foo.py"],
        specialty="logic",
    )
    state = _minimal_state()
    ctx = ReviewTaskContext(file_snippets={"foo.py": "x"})
    packet = build_critique_probe_packet(state, task, ctx, code_evidence="ev")
    principles = next(s for s in packet.sections if s.key == "review_principles")
    assert IN_FUNCTION_CONTRACT_GUIDANCE[:40] in principles.content
    assert principles_for_specialty("logic") == principles.content


def test_critique_probe_packet_sections_stable() -> None:
    task = ReviewTask(
        id="t1",
        title="T",
        description="D",
        target_files=["foo.py"],
    )
    state = _minimal_state()
    ctx = ReviewTaskContext(file_snippets={"foo.py": "code"})
    packet = build_critique_probe_packet(state, task, ctx)
    keys = sorted(s.key for s in packet.sections)
    assert keys == ["code_evidence", "review_principles"]
    flags = merge_probe_flags(packet)
    assert flags["packet_version"] == "0"
    assert "sections_included" in flags


def test_plan_critic_packet_excludes_full_spec_json() -> None:
    from unittest.mock import patch

    spec = BehavioralSpec(
        intent_summary="intent",
        contract_boundaries="SECRET_CONTRACT_BOUNDARY_MARKER_XYZ",
        risk_hypotheses="maybe ReDoS",
        uncertainties="registry unknown",
    )
    state = _minimal_state(behavioral_spec_ref="spec-ref")
    tasks = [
        ReviewTask(id="t1", title="T", description="D", target_files=["foo.py"]),
    ]
    with patch(
        "src.orchestration.context.context_packets.BehavioralSpecStore"
    ) as store_cls:
        store_cls.return_value.read.return_value = spec
        packet = build_plan_critic_packet(state, tasks)
    blob = "\n".join(s.content for s in packet.sections)
    assert "SECRET_CONTRACT_BOUNDARY_MARKER_XYZ" not in blob
    assert "ReDoS" in blob or "maybe" in blob


def test_spec_risks_excerpt_for_prompt_no_contract_dump() -> None:
    from unittest.mock import patch

    spec = BehavioralSpec(
        contract_boundaries="FULL_CONTRACT_SHOULD_NOT_APPEAR",
        risk_hypotheses="risk one",
        uncertainties="uncertainty one",
    )
    with patch(
        "src.orchestration.context.context_packets.BehavioralSpecStore"
    ) as store_cls:
        store_cls.return_value.read.return_value = spec
        text = spec_risks_excerpt_for_prompt("ref")
    assert "FULL_CONTRACT_SHOULD_NOT_APPEAR" not in text
    assert "risk one" in text


def test_revision_shard_excludes_direct_context() -> None:
    candidate = CandidateFinding(
        candidate_id="c1",
        patch_task_id="t1",
        file_path="foo.py",
        line_start=1,
        line_end=2,
        claim_type="defect",
        content="bug",
        failure_mode="crash",
        evidence_summary="see line 1",
        reflection_specialties=["logic"],
    )
    fc = FocusedContextResult(
        request_id="r1",
        candidate_id="c1",
        file_snippets={"foo.py": "snippet"},
        search_hits={
            "q": [
                SearchResult(
                    file_path="foo.py",
                    line_number=1,
                    content="hit",
                    context_lines=[],
                )
            ],
        },
    )
    state = _minimal_state(
        metadata={
            "critique_pipeline": {
                "by_task": {
                    "t1": {"direct_context": "SHOULD_NOT_LEAK_INTO_REVISION"},
                }
            }
        }
    )
    packet = build_critique_revision_shard_packet(
        state,
        candidate=candidate,
        focused_results=[fc],
    )
    blob = "\n".join(s.content for s in packet.sections)
    assert "SHOULD_NOT_LEAK_INTO_REVISION" not in blob
    assert "snippet" in blob


def test_intent_packet_includes_pr_context_when_metadata_set() -> None:
    state = _minimal_state(
        metadata={
            "pr_title": "Add widgets",
            "pr_description": "Introduces Foo and Bar handlers.",
        }
    )
    packet = build_intent_extractor_packet(state)
    keys = {s.key for s in packet.sections}
    assert "pr_context" in keys
    pr = next(s for s in packet.sections if s.key == "pr_context")
    assert "Add widgets" in pr.content
    assert "Foo and Bar" in pr.content


def test_classes_introduced_in_diff_collects_added_classes() -> None:
    diff = (
        "diff --git a/module.py b/module.py\n"
        "+++ b/module.py\n"
        "+class Alpha:\n"
        "+    pass\n"
        "+class Beta:\n"
        "+    pass\n"
        "+class Gamma:\n"
    )
    assert classes_introduced_in_diff(diff) == ["Alpha", "Beta", "Gamma"]


def test_enrich_intent_summary_appends_when_scope_incomplete() -> None:
    diff = "\n".join(f"+class C{i}:\n+    pass" for i in range(5))
    summary = "Adds C1 only."
    enriched, warnings = enrich_intent_summary_with_diff_scope(summary, diff)
    assert "Surfaces introduced in diff" in enriched
    assert "C4" in enriched
    assert any("intent_scope_incomplete" in w for w in warnings)


def test_plan_critic_packet_shrinks_diff_after_bootstrap() -> None:
    huge_diff = "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n" + ("+line\n" * 5000)
    state = _minimal_state(
        git_diff=huge_diff,
        metadata={"mental_model": {"bootstrap_completed": True}},
    )
    tasks = [ReviewTask(id="t1", title="T", description="D", target_files=["foo.py"])]
    packet = build_plan_critic_packet(state, tasks)
    diff_sec = next(s for s in packet.sections if s.key == "diff_summary")
    assert len(diff_sec.content) <= 2500


def test_mandate_explorer_git_diff_cap_shrinks_after_full_file_read() -> None:
    from src.orchestration.context.mandate_loop_context import mandate_explorer_git_diff_cap

    diff = (
        "diff --git a/comfy_extras/nodes_string.py b/comfy_extras/nodes_string.py\n"
        "+++ b/comfy_extras/nodes_string.py\n"
        "+class RegexExtract:\n+    pass\n"
    )
    state = _minimal_state(
        git_diff=diff,
        exploration_ledger=[
            {
                "kind": "mandate_tool_observation",
                "tool": "read_file",
                "args_preview": "file_path='comfy_extras/nodes_string.py', full_file=True",
            }
        ],
    )
    assert mandate_explorer_git_diff_cap(state) == 4000
    assert mandate_explorer_git_diff_cap(_minimal_state(git_diff=diff)) == 10_000


def test_reflection_packet_omits_diff_when_files_complete() -> None:
    candidate = CandidateFinding(
        candidate_id="c1",
        patch_task_id="t1",
        file_path="foo.py",
        line_start=1,
        line_end=2,
        claim_type="defect",
        content="missing return path",
        failure_mode="wrong_output",
        evidence_summary="line 1",
        reflection_specialties=["logic"],
    )
    state = _minimal_state(
        metadata={
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": {
                            "files_complete": {"foo.py": True},
                            "file_contents": {"foo.py": "def execute():\n    return None\n"},
                        }
                    }
                }
            }
        }
    )
    packet = build_reflection_packet(state, "logic", [candidate])
    keys = {s.key for s in packet.sections}
    assert "diff_hunk" not in keys
    code = next(s for s in packet.sections if s.key == "code_evidence")
    assert "execute" in code.content


def test_reflection_packet_omits_redundant_claim_slice_when_class_cited() -> None:
    file_body = (
        "class StringTrim:\n"
        "    def execute(self, mode):\n"
        "        if mode == 'Both':\n"
        "            return 'x'\n"
        "        return None\n"
    )
    candidate = CandidateFinding(
        candidate_id="c1",
        patch_task_id="t1",
        file_path="nodes.py",
        line_start=2,
        line_end=3,
        claim_type="defect",
        content="StringTrim.execute(mode) missing else on mode",
        failure_mode="wrong_output",
        evidence_summary="execute",
        reflection_specialties=["logic"],
    )
    state = _minimal_state(
        metadata={
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": {
                            "file_contents": {"nodes.py": file_body},
                        }
                    }
                }
            }
        }
    )
    packet = build_reflection_packet(state, "logic", [candidate])
    code = next(s for s in packet.sections if s.key == "code_evidence")
    assert "Cited class" in code.content
    assert "Claim slice" not in code.content


def test_revision_shard_omits_diff_when_file_complete() -> None:
    candidate = CandidateFinding(
        candidate_id="c1",
        patch_task_id="t1",
        file_path="comfy_extras/nodes_string.py",
        line_start=10,
        line_end=12,
        claim_type="defect",
        content="RegexExtract execute missing branch",
        failure_mode="wrong_output",
        evidence_summary="see execute",
        reflection_specialties=["logic"],
    )
    state = _minimal_state(
        git_diff="diff --git a/comfy_extras/nodes_string.py b/comfy_extras/nodes_string.py\n+++ b/comfy_extras/nodes_string.py\n" + ("+x\n" * 8000),
        metadata={
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": {
                            "files_complete": {"comfy_extras/nodes_string.py": True},
                            "file_contents": {
                                "comfy_extras/nodes_string.py": (
                                    "class RegexExtract:\n"
                                    "    def execute(self, mode, pattern):\n"
                                    "        return None\n"
                                ),
                            },
                        }
                    }
                }
            }
        },
    )
    packet = build_critique_revision_shard_packet(
        state,
        candidate=candidate,
        focused_results=[],
    )
    keys = {s.key for s in packet.sections}
    assert "diff_hunk" not in keys
    assert packet.metadata.get("diff_hunk_suppressed") is True
    evidence = next(s for s in packet.sections if s.key == "revision_shard_evidence")
    assert "RegexExtract" in evidence.content
