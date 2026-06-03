from __future__ import annotations

import pytest

from src.config import get_settings
from src.domain.schemas import (
    CandidateFinding,
    CritiqueRevisionDigest,
    FocusedContextResult,
    ReflectionReport,
    ReviewAdjudicationItem,
    ReviewAdjudicationOutput,
    ReviewCheckResult,
    ReviewFinding,
)
from src.domain.verifier_schemas import VerifierReport
from src.orchestration.nodes.application.review_adjudicator import (
    _normalize_adjudication_items,
    build_review_adjudication_packets,
    plan_adjudication_batches,
)


def _candidate(candidate_id: str = "c1", *, file_path: str = "pkg/mod.py") -> CandidateFinding:
    return CandidateFinding(
        candidate_id=candidate_id,
        patch_task_id="task",
        file_path=file_path,
        line_start=10,
        line_end=12,
        content="Changed operation can return the wrong value.",
        claim_type="defect",
        failure_mode="wrong output",
        evidence_summary="local evidence",
        recommendation="preserve the expected value",
        suspected_category="logic",
        reflection_specialties=["logic"],
        evidence_for_contract="The function previously returned the complete value.",
        counterexample="Calling the changed path loses part of the value.",
        rejection_check="No caller guarantee rules out this path.",
    )


def _finding(candidate_id: str = "c1") -> ReviewFinding:
    return ReviewFinding(
        id=candidate_id,
        file_path="pkg/mod.py",
        line_start=10,
        line_end=12,
        content="Changed operation can return the wrong value.",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="preserve the expected value",
    )


def test_adjudication_packet_includes_available_evidence() -> None:
    candidate = _candidate()
    check_result = ReviewCheckResult(
        check_id="check-1",
        patch_task_id="task",
        decision="candidate",
        reportable_reason="same claim evidence",
        candidate=candidate,
    )
    state = {
        "run_id": "r",
        "repo_path": "/repo",
        "git_diff": "diff --git a/pkg/mod.py b/pkg/mod.py",
        "candidate_findings": [candidate],
        "review_check_results": [check_result],
        "reflection_reports": [
            ReflectionReport(
                candidate_id="c1",
                reflector_specialty="logic",
                verdict="accept",
                rationale="local support",
            )
        ],
        "focused_context_results": {
            "focus-1": FocusedContextResult(
                request_id="focus-1",
                candidate_id="c1",
                file_snippets={"pkg/mod.py": "def changed(): ..."},
            )
        },
        "verifier_reports": [
            VerifierReport(
                run_id="r",
                candidate_id="c1",
                verdict="verified",
                final_rationale="verified concrete behavior",
            )
        ],
        "critique_revision_digests": {
            "d1": CritiqueRevisionDigest(
                shard_id="d1",
                candidate_id="c1",
                evidence_bullets=["focused evidence supports the claim"],
                impact="supports",
            )
        },
        "metadata": {
            "verifier_hints": {"c1": {"verdict": "verified"}},
            "critique_revision": {
                "revisions": [
                    {
                        "candidate_id": "c1",
                        "verdict": "accept",
                        "updated_evidence_summary": "supported",
                    }
                ]
            },
            "adversarial_cleanup": {
                "candidate_lifecycle": {
                    "c1": {"decision": "dropped", "reason": "legacy advisory"}
                }
            },
        },
    }

    packets = build_review_adjudication_packets(state)  # type: ignore[arg-type]

    assert len(packets) == 1
    packet = packets[0]
    assert packet["candidate"]["candidate_id"] == "c1"
    assert packet["originating_checks"][0]["check_id"] == "check-1"
    assert packet["reflection_reports"][0]["verdict"] == "accept"
    assert packet["focused_context"][0]["file_snippets"]
    assert packet["verifier_reports"][0]["verdict"] == "verified"
    assert packet["verifier_hint"]["verdict"] == "verified"
    assert packet["critique_revision_digests"][0]["impact"] == "supports"
    assert packet["critique_revision_rows"][0]["verdict"] == "accept"
    assert packet["prior_lifecycle_hint"]["reason"] == "legacy advisory"


def test_adjudication_validation_records_one_lifecycle_per_candidate() -> None:
    candidates = {
        "c1": _candidate("c1"),
        "c2": _candidate("c2"),
        "c3": _candidate("c3"),
    }
    output = ReviewAdjudicationOutput(
        items=[
            ReviewAdjudicationItem(
                candidate_id="c1",
                decision="promote",
                finding=_finding("c1"),
                rationale="supported",
            ),
            ReviewAdjudicationItem(
                candidate_id="c2",
                decision="merge",
                merge_into="c1",
                rationale="same claim",
            ),
        ]
    )

    findings, lifecycle, merge_map, warnings = _normalize_adjudication_items(
        output=output,
        candidates=candidates,
        changed_files={"pkg/mod.py"},
    )

    assert [finding.id for finding in findings] == ["c1"]
    assert lifecycle["c1"]["decision"] == "promoted"
    assert lifecycle["c2"]["decision"] == "merged"
    assert lifecycle["c3"]["reason"] == "adjudicator_missing_decision"
    assert merge_map == {"c1": ["c2"]}
    assert "adjudication_missing_candidate:c3" in warnings


def test_adjudication_validation_rejects_invalid_merge_target() -> None:
    candidates = {"c1": _candidate("c1")}
    output = ReviewAdjudicationOutput(
        items=[
            ReviewAdjudicationItem(
                candidate_id="c1",
                decision="merge",
                merge_into="missing",
                rationale="bad target",
            )
        ]
    )

    findings, lifecycle, merge_map, warnings = _normalize_adjudication_items(
        output=output,
        candidates=candidates,
        changed_files={"pkg/mod.py"},
    )

    assert findings == []
    assert merge_map == {}
    assert lifecycle["c1"]["reason"] == "invalid_merge_target"
    assert "adjudication_invalid_merge:c1->missing" in warnings


def test_adjudication_batches_do_not_drop_candidates() -> None:
    packets = [{"candidate": _candidate(f"c{i}").model_dump(mode="json"), "blob": "x" * 2000} for i in range(5)]

    batches = plan_adjudication_batches(
        packets,
        max_batch_chars=2500,
        max_candidate_chars=5000,
    )

    seen = [
        packet["candidate"]["candidate_id"]
        for batch in batches
        for packet in batch
    ]
    assert seen == ["c0", "c1", "c2", "c3", "c4"]
    assert len(batches) > 1


def test_post_reflection_default_routes_to_review_adjudicator(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.orchestration.routing.adversarial_after_reflection import route_focused_after_reflection

    monkeypatch.delenv("REVIEW_REVIEWER_USE_LEGACY_ADVERSARIAL_CLEANUP", raising=False)
    get_settings.cache_clear()
    try:
        assert route_focused_after_reflection({"reflection_reports": []}) == "review_adjudicator"
    finally:
        get_settings.cache_clear()


def test_post_reflection_legacy_setting_routes_to_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.orchestration.routing.adversarial_after_reflection import route_focused_after_reflection

    monkeypatch.setenv("REVIEW_REVIEWER_USE_LEGACY_ADVERSARIAL_CLEANUP", "true")
    get_settings.cache_clear()
    try:
        assert route_focused_after_reflection({"reflection_reports": []}) == "adversarial_cleanup"
    finally:
        monkeypatch.delenv("REVIEW_REVIEWER_USE_LEGACY_ADVERSARIAL_CLEANUP", raising=False)
        get_settings.cache_clear()
