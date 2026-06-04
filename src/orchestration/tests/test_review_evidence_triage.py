from typing import Any

from src.config import Settings
from src.domain.schemas import (
    CandidateFinding,
    ReviewCheck,
    ReviewCheckResult,
    ReviewEvidenceTriageItem,
    ReviewEvidenceTriageOutput,
    SourceFact,
)
from src.orchestration.nodes.application.review_adjudicator import build_review_adjudication_packets
from src.orchestration.nodes.application.review_evidence_triage import (
    build_review_evidence_triage_packets,
    make_review_evidence_triage_node,
)


def _candidate(candidate_id: str, **updates) -> CandidateFinding:
    values = {
        "candidate_id": candidate_id,
        "patch_task_id": "review-logic",
        "file_path": "src/app.py",
        "line_start": 1,
        "line_end": 2,
        "content": "changed behavior claim",
        "claim_type": "defect",
        "expected_behavior": "the changed operation preserves its declared behavior",
        "failure_mode": "changed behavior is incorrect",
        "evidence_summary": "local evidence",
        "suspected_category": "logic",
        "reflection_specialties": ["logic"],
    }
    values.update(updates)
    return CandidateFinding(**values)  # type: ignore[arg-type]


def test_triage_records_every_candidate_without_keyword_family_rules() -> None:
    candidates = [
        _candidate("c1", claim_type="security_risk", reflection_specialties=["security", "logic"]),
        _candidate("c2", claim_type="missing_test", reflection_specialties=[]),
    ]

    out = make_review_evidence_triage_node(use_llm=False)({"candidate_findings": candidates})

    triage = out["metadata"]["review_evidence_triage"]
    assert triage["candidate_count"] == 2
    assert [item["candidate_id"] for item in triage["items"]] == ["c1", "c2"]
    assert triage["items"][0]["suggested_reflection_specialties"] == ["security", "logic"]
    assert triage["items"][1]["claim_family"] == "missing_test"
    assert "review_evidence_triage_llm_disabled" in triage["warnings"]


def test_triage_packet_is_compact_and_omits_raw_candidate_dump() -> None:
    candidate = _candidate("c1", recommendation="extra raw field should not be copied")
    check = ReviewCheck(
        check_id="check:1",
        patch_task_id="review-logic",
        file_path="src/app.py",
        behavioral_question="Does the changed operation preserve its result contract?",
        affected_invariant="result contract",
        expected_behavior="the changed operation preserves its declared behavior",
        required_evidence=["changed operation"],
        suppress_criteria=["contract preserved"],
        report_criteria=["contract broken"],
        allowed_retrieval=["task_evidence"],
    )
    result = ReviewCheckResult(
        check_id="check:1",
        patch_task_id="review-logic",
        decision="candidate",
        reportable_reason="contract evidence supports review",
        candidate=candidate,
    )

    packets = build_review_evidence_triage_packets(
        {"candidate_findings": [candidate], "review_checks": [check], "review_check_results": [result]}
    )

    packet = packets[0]
    assert set(packet["candidate"]) == {
        "candidate_id",
        "file_path",
        "line_start",
        "line_end",
        "claim_type",
        "suspected_category",
        "reflection_specialties",
        "content",
        "expected_behavior",
        "failure_mode",
        "evidence_summary",
        "required_context",
    }
    assert "recommendation" not in packet["candidate"]
    assert packet["originating_checks"][0] == {
        "check_id": "check:1",
        "behavioral_question": "Does the changed operation preserve its result contract?",
        "affected_invariant": "result contract",
        "expected_behavior": "the changed operation preserves its declared behavior",
        "decision": "candidate",
        "reportable_reason": "contract evidence supports review",
    }


def test_triage_length_failure_retries_smaller_and_falls_back(monkeypatch) -> None:
    candidates = [_candidate("c1"), _candidate("c2")]
    prompts: list[str] = []

    class LengthFinishReasonError(Exception):
        pass

    class FakeLlm:
        def __init__(self, actions: list[Any]) -> None:
            self.actions = actions

        def invoke(self, prompt: str) -> Any:
            prompts.append(prompt)
            action = self.actions.pop(0)
            if isinstance(action, BaseException):
                raise action
            return action

    actions: list[Any] = [
        LengthFinishReasonError("length limit"),
        ReviewEvidenceTriageOutput(
            items=[
                ReviewEvidenceTriageItem(
                    candidate_id="c1",
                    claim_summary="c1",
                    claim_family="defect",
                    suggested_reflection_specialties=["logic"],
                )
            ]
        ),
        LengthFinishReasonError("length limit again"),
    ]
    fake = FakeLlm(actions)
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_evidence_triage.Models.worker",
        lambda *_args, **_kwargs: fake,
    )

    out = make_review_evidence_triage_node(
        settings=Settings(
            redis_enabled=False,
            reviewer_triage_max_batch_chars=100000,
            reviewer_triage_max_candidate_chars=10000,
        )
    )({"candidate_findings": candidates, "git_diff": "diff --git a/src/app.py b/src/app.py"})

    triage = out["metadata"]["review_evidence_triage"]
    assert triage["failed_batch_candidate_ids"] == ["c1", "c2"]
    assert triage["retried_batch_count"] == 2
    assert triage["retry_success_count"] == 1
    assert triage["fallback_candidate_ids"] == ["c2"]
    assert len(prompts) == 3
    assert "diff --git" not in prompts[0]


def test_adjudicator_packet_includes_triage_and_source_facts() -> None:
    candidate = _candidate("c1")
    fact = SourceFact(
        candidate_id="c1",
        fact_kind="reachable_fallthrough",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        summary="execute has a path without return",
        evidence="def execute(mode): ...",
    )
    state = {
        "candidate_findings": [candidate],
        "source_facts": [fact],
        "metadata": {
            "review_evidence_triage": {
                "items": [
                    {
                        "candidate_id": "c1",
                        "claim_summary": "changed behavior claim",
                        "claim_family": "defect",
                        "suggested_reflection_specialties": ["logic", "general"],
                        "source_fact_requests": ["return path facts"],
                        "runtime_verification_usefulness": "advisory",
                        "needed_context": [],
                        "rationale": "Declared candidate fields support logic review.",
                    }
                ]
            }
        },
    }

    packets = build_review_adjudication_packets(state)  # type: ignore[arg-type]

    assert packets[0]["candidate"]["candidate_id"] == "c1"
    assert packets[0]["triage"]["source_fact_requests"] == ["return path facts"]
    assert packets[0]["source_facts"][0]["fact_kind"] == "reachable_fallthrough"


def test_adjudicator_packet_includes_focused_context_diagnostics() -> None:
    candidate = _candidate("c1")
    state = {
        "candidate_findings": [candidate],
        "metadata": {
            "focused_context": {
                "diagnostics": [
                    {
                        "request_id": "r1",
                        "candidate_id": "c1",
                        "requested_paths": ["src/app.py"],
                        "effective_paths": [],
                        "outcomes": ["no_hits"],
                    }
                ]
            }
        },
    }

    packets = build_review_adjudication_packets(state)  # type: ignore[arg-type]

    assert packets[0]["focused_context_diagnostics"][0]["outcomes"] == ["no_hits"]
