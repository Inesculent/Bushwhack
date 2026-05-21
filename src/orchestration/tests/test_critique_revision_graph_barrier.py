"""Graph barrier helpers for critique revision map-reduce."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding, FocusedContextResult, ReflectionReport
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierReport
from src.orchestration.nodes.application.critique_revision import (
    critique_revision_digests_complete,
    expected_critique_revision_shard_ids,
    revision_inputs_ready,
    revision_ready_candidate_ids,
)


def _state(**kwargs) -> GraphState:
    base: GraphState = {
        "run_id": "t",
        "repo_path": "/repo",
        "git_diff": "diff",
        "candidate_findings": [],
        "reflection_reports": [],
        "focused_context_results": {},
        "critique_revision_digests": {},
        "metadata": {},
    }
    base.update(kwargs)  # type: ignore[typeddict-item]
    return base


def test_critique_revision_digests_complete_when_all_shards_present() -> None:
    cid = "c1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="m.py",
        line_start=1,
        line_end=2,
        content="x",
        claim_type="defect",
        failure_mode="crash",
    )
    rep = ReflectionReport(
        candidate_id=cid,
        reflector_specialty="logic",
        verdict="needs_context",
        focused_request=None,
    )
    fc = FocusedContextResult(
        request_id="r1",
        candidate_id=cid,
        file_snippets={"m.py": "code"},
    )
    state = _state(
        candidate_findings=[cand],
        reflection_reports=[rep],
        focused_context_results={"r1": fc},
    )
    expected = expected_critique_revision_shard_ids(state, [cid])
    assert expected
    assert not critique_revision_digests_complete(state)
    state["critique_revision_digests"] = {sid: {"shard_id": sid} for sid in expected}
    assert critique_revision_digests_complete(state)


def test_critique_revision_digests_incomplete_with_partial_shards() -> None:
    cid = "c1"
    state = _state(
        candidate_findings=[
            CandidateFinding(
                candidate_id=cid,
                patch_task_id="t1",
                file_path="m.py",
                line_start=1,
                line_end=2,
                content="x",
                claim_type="defect",
                failure_mode="crash",
            )
        ],
        reflection_reports=[
            ReflectionReport(
                candidate_id=cid,
                reflector_specialty="logic",
                verdict="needs_context",
            )
        ],
        focused_context_results={
            "r1": FocusedContextResult(
                request_id="r1",
                candidate_id=cid,
                file_snippets={"m.py": "x"},
            )
        },
        critique_revision_digests={"only_one_shard": {"shard_id": "only_one_shard"}},
    )
    expected = expected_critique_revision_shard_ids(state, [cid])
    if len(expected) <= 1:
        return
    assert not critique_revision_digests_complete(state)


def test_revision_ready_candidate_ids_partial_batch() -> None:
    """Verifier-backed needs_verification is ready; needs_context without focus does not block siblings."""
    ready_cid = "logic-string-nodes-comprehensive-6"
    blocked_cid = "logic-string-nodes-comprehensive-7"
    ready_cand = CandidateFinding(
        candidate_id=ready_cid,
        patch_task_id="t1",
        file_path="comfy_extras/nodes_string.py",
        line_start=217,
        line_end=227,
        content="All Groups",
        claim_type="defect",
        failure_mode="group 0",
    )
    blocked_cand = CandidateFinding(
        candidate_id=blocked_cid,
        patch_task_id="t1",
        file_path="comfy_extras/nodes_string.py",
        line_start=180,
        line_end=190,
        content="try:",
        claim_type="defect",
        failure_mode="silent regex error",
    )
    state = _state(
        candidate_findings=[ready_cand, blocked_cand],
        reflection_reports=[
            ReflectionReport(
                candidate_id=ready_cid,
                reflector_specialty="logic",
                verdict="needs_verification",
            ),
            ReflectionReport(
                candidate_id=blocked_cid,
                reflector_specialty="logic",
                verdict="needs_context",
            ),
        ],
        verifier_reports=[
            VerifierReport(
                run_id="r1",
                candidate_id=ready_cid,
                verdict="verified",
                final_rationale="repro",
                updated_evidence_summary="verified",
            )
        ],
    )
    all_ids = [ready_cid, blocked_cid]
    assert revision_ready_candidate_ids(state, all_ids) == [ready_cid]
    assert revision_inputs_ready(state, all_ids) is True
    assert blocked_cid not in revision_ready_candidate_ids(state, all_ids)


def test_critique_revision_reduce_waits_until_all_digest_shards_present() -> None:
    from src.orchestration.nodes.application.critique_revision import (
        make_critique_revision_reduce_node,
    )

    cid = "c1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="m.py",
        line_start=1,
        line_end=2,
        content="x",
        claim_type="defect",
        failure_mode="crash",
    )
    rep = ReflectionReport(
        candidate_id=cid,
        reflector_specialty="logic",
        verdict="needs_verification",
    )
    fc = FocusedContextResult(
        request_id="r1",
        candidate_id=cid,
        file_snippets={"m.py": "code"},
    )
    state = _state(
        candidate_findings=[cand],
        reflection_reports=[rep],
        focused_context_results={"r1": fc},
        verifier_reports=[],
    )
    node = make_critique_revision_reduce_node(use_llm=False)
    out_early = node(state)
    assert "barrier_incomplete" in out_early["node_history"][0]

    expected = expected_critique_revision_shard_ids(state, [cid])
    state["critique_revision_digests"] = {sid: {"shard_id": sid} for sid in expected}
    out_done = node(state)
    assert out_done["node_history"][0] == "critique_revision_reduce"
    assert out_done["metadata"]["critique_revision"]["reduce_completed"] is True
