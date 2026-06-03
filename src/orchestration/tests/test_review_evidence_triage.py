from src.domain.schemas import CandidateFinding, SourceFact
from src.orchestration.nodes.application.review_adjudicator import build_review_adjudication_packets
from src.orchestration.nodes.application.review_evidence_triage import make_review_evidence_triage_node


def _candidate(candidate_id: str, **updates) -> CandidateFinding:
    values = {
        "candidate_id": candidate_id,
        "patch_task_id": "review-logic",
        "file_path": "src/app.py",
        "line_start": 1,
        "line_end": 2,
        "content": "changed behavior claim",
        "claim_type": "defect",
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
