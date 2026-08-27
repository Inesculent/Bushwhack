from __future__ import annotations

from pathlib import Path

import pytest

from src.config import get_settings
from src.domain.schemas import (
    CandidateFinding,
    CritiqueRevisionDigest,
    FocusedContextResult,
    ReflectionReport,
    ReviewAdjudicationItem,
    ReviewAdjudicationOutput,
    ReviewCheck,
    ReviewCheckResult,
    ReviewFinding,
)
from src.domain.verifier_schemas import VerifierAttemptRecord, VerifierReport
from src.orchestration.nodes.application.review_adjudicator import (
    _normalize_adjudication_items,
    _packet_evidence_summary,
    _render_reduce_prompt,
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
        expected_behavior="The changed operation returns the complete expected value.",
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
        expected_behavior="The changed operation returns the complete expected value.",
    )


def _normalize(
    output: ReviewAdjudicationOutput | None,
    candidates: dict[str, CandidateFinding],
    *,
    changed_files: set[str] | None = None,
) -> tuple[list[ReviewFinding], dict[str, object], dict[str, list[str]], list[str]]:
    findings, lifecycle, merge_map, warnings, _verification_requested = _normalize_adjudication_items(
        output=output,
        candidates=candidates,
        changed_files=changed_files or {"pkg/mod.py"},
    )
    return findings, lifecycle, merge_map, warnings


def test_adjudication_packet_includes_available_evidence(tmp_path: Path) -> None:
    repo_file = tmp_path / "pkg" / "mod.py"
    repo_file.parent.mkdir()
    repo_file.write_text(
        "\n".join(
            [
                "line = 1",
                "line = 2",
                "line = 3",
                "line = 4",
                "line = 5",
                "line = 6",
                "line = 7",
                "line = 8",
                "line = 9",
                "def changed():",
                "    value = incomplete()",
                "    return value",
            ]
        )
    )
    candidate = _candidate()
    check_result = ReviewCheckResult(
        check_id="check-1",
        patch_task_id="task",
        decision="candidate",
        reportable_reason="same claim evidence",
        candidate=candidate,
    )
    check = ReviewCheck(
        check_id="check-1",
        patch_task_id="task",
        file_path="pkg/mod.py",
        line_start=10,
        line_end=12,
        changed_code_anchor="changed",
        owned_contract_scope="pkg/mod.py:changed:return-complete-value",
        behavioral_question="Does the changed operation return the complete expected value?",
        affected_invariant="complete return value",
        expected_behavior="The changed operation returns the complete expected value.",
        required_evidence=["changed return path"],
        suppress_criteria=["all returned values are complete"],
        report_criteria=["a path returns an incomplete value"],
        allowed_retrieval=["task_evidence"],
    )
    state = {
        "run_id": "r",
        "repo_path": str(tmp_path),
        "git_diff": "diff --git a/pkg/mod.py b/pkg/mod.py",
        "candidate_findings": [candidate],
        "review_checks": [check],
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
    assert packet["evidence_card"]["source_lines"]["status"] == "included"
    assert packet["evidence_card"]["source_lines"]["origin"] == "repo_path"
    assert "10: def changed():" in packet["evidence_card"]["source_lines"]["snippet"]
    assert packet["candidate"]["candidate_id"] == "c1"
    assert packet["candidate"]["expected_behavior"] == "The changed operation returns the complete expected value."
    assert packet["originating_checks"][0]["check_id"] == "check-1"
    assert (
        packet["originating_checks"][0]["originating_check"]["owned_contract_scope"]
        == "pkg/mod.py:changed:return-complete-value"
    )
    assert "complete expected value" in packet["originating_checks"][0]["originating_check"]["behavioral_question"]
    assert packet["reflection_reports"][0]["verdict"] == "accept"
    assert packet["focused_context"][0]["file_snippets"]
    assert packet["verifier_reports"][0]["verdict"] == "verified"
    assert packet["verifier_hint"]["verdict"] == "verified"
    assert packet["critique_revision_digests"][0]["impact"] == "supports"
    assert packet["critique_revision_rows"][0]["verdict"] == "accept"
    assert packet["prior_lifecycle_hint"]["reason"] == "legacy advisory"


def test_adjudication_packet_uses_task_evidence_when_repo_path_is_remote() -> None:
    candidate = _candidate()
    lines = [f"line = {index}" for index in range(1, 10)] + [
        "def changed():",
        "    value = incomplete()",
        "    return value",
    ] + [f"trailing = {index}" for index in range(13, 21)]
    state = {
        "repo_path": "https://github.com/example/project",
        "candidate_findings": [candidate],
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "task": {
                        "task_evidence": {
                            "file_contents": {"pkg/mod.py": "\n".join(lines)}
                        }
                    }
                }
            }
        },
    }

    packet = build_review_adjudication_packets(state)[0]  # type: ignore[arg-type]
    source = packet["evidence_card"]["source_lines"]

    assert source["status"] == "included"
    assert source["origin"] == "task_evidence"
    assert "10: def changed():" in source["snippet"]
    assert "20: trailing = 20" in source["snippet"]
    assert len(source["snippet"]) <= 1200


def test_adjudication_packet_includes_one_cited_contract_excerpt() -> None:
    candidate = _candidate()
    check_result = ReviewCheckResult(
        check_id="check-1",
        patch_task_id="task",
        decision="candidate",
        evidence_refs=["pkg/mod.py:10-12", "pkg/caller.py:3-4", "pkg/other.py:1"],
        candidate=candidate,
    )
    state = {
        "repo_path": "https://github.com/example/project",
        "candidate_findings": [candidate],
        "review_check_results": [check_result],
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "task": {
                        "task_evidence": {
                            "file_contents": {
                                "pkg/mod.py": "\n".join(f"source {index}" for index in range(1, 20)),
                                "pkg/caller.py": "setup\nvalue = 1\nresult = changed()\nuse(result)\n",
                                "pkg/other.py": "unused\n",
                            }
                        }
                    }
                }
            }
        },
    }

    packet = build_review_adjudication_packets(state)[0]  # type: ignore[arg-type]
    contract = packet["evidence_card"]["contract_lines"]

    assert contract["status"] == "included"
    assert contract["origin"] == "task_evidence"
    assert contract["file_path"] == "pkg/caller.py"
    assert contract["evidence_ref"] == "pkg/caller.py:3-4"
    assert "3: result = changed()" in contract["snippet"]
    assert len(contract["snippet"]) <= 900
    assert _packet_evidence_summary([packet]) == {
        "source_included": 1,
        "source_unavailable": 0,
        "contract_included": 1,
        "source_origins": {"task_evidence": 1},
    }


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

    findings, lifecycle, merge_map, warnings = _normalize(output, candidates)

    assert [finding.id for finding in findings] == ["c1"]
    assert lifecycle["c1"]["decision"] == "promoted"
    assert lifecycle["c2"]["decision"] == "merged"
    assert lifecycle["c3"]["reason"] == "adjudication_item_missing"
    assert merge_map == {"c1": ["c2"]}
    assert "adjudication_missing_candidate_dropped:c3" in warnings


def test_adjudication_none_output_does_not_publish_unadjudicated_findings() -> None:
    candidates = {
        "c1": _candidate("c1"),
        "c2": _candidate("c2"),
    }

    findings, lifecycle, merge_map, warnings = _normalize(None, candidates)

    assert findings == []
    assert lifecycle["c1"]["decision"] == "dropped"
    assert lifecycle["c2"]["decision"] == "dropped"
    assert merge_map == {}
    assert "adjudication_missing_candidate_dropped:c1" in warnings
    assert "adjudication_missing_candidate_dropped:c2" in warnings


def test_adjudication_explicit_drop_records_obvious_drop_reason() -> None:
    candidates = {"c1": _candidate("c1")}
    output = ReviewAdjudicationOutput(
        items=[
            ReviewAdjudicationItem(
                candidate_id="c1",
                decision="drop",
                rationale="Packet is positive-only and has no actionable negative claim.",
            )
        ]
    )

    findings, lifecycle, merge_map, warnings = _normalize(output, candidates)

    assert findings == []
    assert merge_map == {}
    assert warnings == []
    assert lifecycle["c1"]["decision"] == "dropped"
    assert lifecycle["c1"]["reason"] == "adjudicator_drop_obvious"


def test_adjudication_does_not_drop_on_inconclusive_harness_error_verifier() -> None:
    candidates = {"c1": _candidate("c1")}
    output = ReviewAdjudicationOutput(
        items=[
            ReviewAdjudicationItem(
                candidate_id="c1",
                decision="drop",
                rationale="Verifier was inconclusive due to harness errors, and the claim may be standard behavior.",
            )
        ]
    )
    verifier = VerifierReport(
        run_id="run",
        candidate_id="c1",
        verdict="inconclusive",
        attempts=[
            VerifierAttemptRecord(
                attempt_number=1,
                exit_code=2,
                stdout="STATUS: HARNESS_ERROR | Import error: No module named 'typing_extensions'",
                failure_class="module_not_found",
            )
        ],
        metadata={"harness_error": True},
    )

    findings, lifecycle, merge_map, warnings, requested = _normalize_adjudication_items(
        output=output,
        candidates=candidates,
        changed_files={"pkg/mod.py"},
        verifier_report_ids={"c1"},
        verifier_reports_by_candidate={"c1": [verifier]},
    )

    assert [finding.id for finding in findings] == ["c1"]
    assert merge_map == {}
    assert requested == []
    assert lifecycle["c1"]["decision"] == "promoted"
    assert lifecycle["c1"]["warnings"] == [
        "adjudication_drop_overridden_inconclusive_harness_error:c1"
    ]
    assert warnings == ["adjudication_drop_overridden_inconclusive_harness_error:c1"]


def test_adjudication_can_request_one_runtime_verification() -> None:
    candidates = {"c1": _candidate("c1")}
    output = ReviewAdjudicationOutput(
        items=[
            ReviewAdjudicationItem(
                candidate_id="c1",
                decision="verify",
                rationale="A short execution can decide the disputed output semantics.",
            )
        ]
    )

    findings, lifecycle, merge_map, warnings, requested = _normalize_adjudication_items(
        output=output,
        candidates=candidates,
        changed_files={"pkg/mod.py"},
        allow_verification=True,
    )

    assert findings == []
    assert merge_map == {}
    assert warnings == []
    assert requested == ["c1"]
    assert lifecycle["c1"]["decision"] == "verification_requested"


def test_adjudication_cannot_repeat_verification_after_report() -> None:
    candidates = {"c1": _candidate("c1")}
    output = ReviewAdjudicationOutput(
        items=[ReviewAdjudicationItem(candidate_id="c1", decision="verify", rationale="retry")]
    )

    findings, lifecycle, _merge_map, warnings, requested = _normalize_adjudication_items(
        output=output,
        candidates=candidates,
        changed_files={"pkg/mod.py"},
        allow_verification=True,
        verifier_report_ids={"c1"},
    )

    assert findings == []
    assert requested == []
    assert lifecycle["c1"]["reason"] == "verification_unavailable"
    assert "adjudication_verification_unavailable:c1" in warnings


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

    findings, lifecycle, merge_map, warnings = _normalize(output, candidates)

    assert findings == []
    assert merge_map == {}
    assert lifecycle["c1"]["reason"] == "invalid_merge_target"
    assert "adjudication_invalid_merge:c1->missing" in warnings


def test_adjudication_promote_outside_changed_files_still_drops() -> None:
    candidates = {"c1": _candidate("c1")}
    output = ReviewAdjudicationOutput(
        items=[
            ReviewAdjudicationItem(
                candidate_id="c1",
                decision="promote",
                finding=_finding("c1").model_copy(update={"file_path": "pkg/other.py"}),
                rationale="supported",
            )
        ]
    )

    findings, lifecycle, _merge_map, warnings = _normalize(output, candidates)

    assert findings == []
    assert lifecycle["c1"]["reason"] == "promoted_path_not_changed"
    assert "adjudication_promote_outside_changed_files:c1:pkg/other.py" in warnings


def test_adjudication_invalid_promoted_line_range_still_drops() -> None:
    candidates = {"c1": _candidate("c1")}
    output = ReviewAdjudicationOutput(
        items=[
            ReviewAdjudicationItem(
                candidate_id="c1",
                decision="promote",
                finding=_finding("c1").model_copy(update={"line_start": 20, "line_end": 10}),
                rationale="supported",
            )
        ]
    )

    findings, lifecycle, _merge_map, warnings = _normalize(output, candidates)

    assert findings == []
    assert lifecycle["c1"]["reason"] == "invalid_line_range"
    assert "adjudication_invalid_line_range:c1" in warnings


def test_adjudication_requires_explicit_decision_for_combo_uncertainty() -> None:
    candidate = _candidate("c1").model_copy(
        update={
            "content": "StringCompare can implicitly return None for an unexpected COMBO mode.",
            "failure_mode": "implicit None return",
            "evidence_summary": "Local source shows no terminal fallback return.",
            "evidence_for_contract": "RETURN_TYPES declares IO.BOOLEAN for execute.",
            "counterexample": "mode='Unexpected' reaches function end and returns None.",
            "rejection_check": "No packet evidence proves COMBO validation before direct execute calls.",
        }
    )

    findings, lifecycle, _merge_map, warnings = _normalize(None, {"c1": candidate})

    assert findings == []
    assert lifecycle["c1"]["reason"] == "adjudication_item_missing"
    assert "adjudication_missing_candidate_dropped:c1" in warnings


def test_adjudication_requires_explicit_decision_for_data_loss_candidate() -> None:
    candidate = _candidate("c1").model_copy(
        update={
            "content": "All Matches extracts only m[0] from findall tuples, dropping other captured groups.",
            "failure_mode": "captured group data loss",
            "evidence_summary": "re.findall returns tuples for multiple capturing groups.",
            "evidence_for_contract": "All Matches should preserve all matched group data unless narrowed.",
            "counterexample": "pattern='(a)(b)' returns [('a', 'b')] but output keeps only 'a'.",
            "rejection_check": "The packet contains no proof that only the first group is intended.",
        }
    )

    findings, lifecycle, _merge_map, warnings = _normalize(None, {"c1": candidate})

    assert findings == []
    assert lifecycle["c1"]["reason"] == "adjudication_item_missing"
    assert "adjudication_missing_candidate_dropped:c1" in warnings


def test_review_adjudicator_prompt_is_balanced_and_evidence_led() -> None:
    prompt = Path("src/orchestration/prompts/reviewer/review_adjudicator.md").read_text()

    assert "Do not default to either promotion or rejection" in prompt
    assert "Default to `promote`" not in prompt
    assert "framework, enum, schema, caller, or runtime might prevent the trigger" in prompt
    assert "A schema allowing a value proves that trigger can exist" in prompt
    assert "global Git Diff Excerpt is supplementary" in prompt
    assert "base, abstract, or default hook" in prompt
    assert "Merge only true duplicates with the same expected behavior, contract, operation, trigger, and impact" in prompt


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


def test_adjudication_reduce_prompt_includes_candidate_comparison_roster() -> None:
    candidates = {
        "c1": _candidate("c1").model_copy(update={"claim_digest": "same-root"}),
        "c2": _candidate("c2").model_copy(update={"claim_digest": "same-root"}),
    }

    prompt = _render_reduce_prompt(
        {"git_diff": "diff --git a/pkg/mod.py b/pkg/mod.py"},
        ["c1", "c2"],
        [
            {
                "items": [
                    {"candidate_id": "c1", "decision": "promote", "rationale": "batch one"},
                    {"candidate_id": "c2", "decision": "promote", "rationale": "batch two"},
                ]
            }
        ],
        candidates,
    )

    assert "candidate_comparison_roster" in prompt
    assert "same-root" in prompt
    assert "c1" in prompt and "c2" in prompt


def test_post_reflection_default_routes_to_review_adjudicator() -> None:
    from src.orchestration.routing.adversarial_after_reflection import route_focused_after_reflection

    assert route_focused_after_reflection({"reflection_reports": []}) == "review_adjudicator"
