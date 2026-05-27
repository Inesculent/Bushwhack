from __future__ import annotations

from src.domain.schemas import AuditCoverageRecord, CandidateFinding, ReviewTask
from src.orchestration.routing.review_obligations import (
    derive_review_obligations,
    evaluate_review_obligations,
)


def test_obligation_evaluation_marks_candidate_audit_and_context_gap() -> None:
    task = ReviewTask(
        id="logic-structured",
        title="Structured handler review",
        description="Audit branch handling and structured result preservation.",
        target_files=["pkg/h.py"],
        specialty="logic",
    )
    evidence = {
        "file_contents": {
            "pkg/h.py": "\n".join(
                [
                    "def execute(mode, rows):",
                    "    if mode == 'a':",
                    "        return rows[0]",
                    "    elif mode == 'b':",
                    "        return '\\n'.join([r[0] for r in rows])",
                ]
            )
        },
        "files_complete": {"pkg/h.py": False},
    }
    obligations = derive_review_obligations(task, evidence)
    assert {row["dimension"] for row in obligations} >= {
        "branch exhaustiveness",
        "structured data preservation",
        "aggregation/serialization safety",
    }

    candidate = CandidateFinding(
        candidate_id="logic-structured-1",
        patch_task_id=task.id,
        file_path="pkg/h.py",
        line_start=1,
        line_end=5,
        content="The handler keeps only the first slot from structured rows.",
        claim_type="defect",
        failure_mode="Data loss from first-slot retention.",
        evidence_summary="The row normalization indexes [0].",
        recommendation="Preserve all relevant slots or narrow the contract.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    audit = AuditCoverageRecord(
        surface="execute",
        dimensions=["branch exhaustiveness"],
        notes="Branches inspected; no supported fall-through claim emitted.",
    )

    evaluated = evaluate_review_obligations(obligations, [candidate], [audit])
    statuses = {row["dimension"]: row["status"] for row in evaluated["obligations"]}
    assert statuses["structured data preservation"] == "candidate"
    assert statuses["branch exhaustiveness"] == "cleared_with_evidence"
    assert statuses["aggregation/serialization safety"] == "needs_context"


def test_obligations_stay_within_task_files_and_candidate_file() -> None:
    task = ReviewTask(
        id="logic-task",
        title="Review one handler",
        description="Audit one changed handler.",
        target_files=["pkg/target.py"],
        specialty="logic",
    )
    evidence = {
        "file_contents": {
            "pkg/target.py": "def execute(rows):\n    return rows[0]\n",
            "pkg/unrelated.py": "def execute(rows):\n    return '\\n'.join(rows)\n",
        },
        "files_complete": {"pkg/target.py": True, "pkg/unrelated.py": True},
    }
    obligations = derive_review_obligations(task, evidence)
    assert {row["file_path"] for row in obligations} == {"pkg/target.py"}

    candidate = CandidateFinding(
        candidate_id="other-file",
        patch_task_id=task.id,
        file_path="pkg/other.py",
        line_start=1,
        line_end=2,
        content="The handler keeps only the first slot.",
        claim_type="defect",
        failure_mode="Data loss from first-slot retention.",
        evidence_summary="The row normalization indexes [0].",
        recommendation="Preserve all relevant slots.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    evaluated = evaluate_review_obligations(obligations, [candidate], [])
    assert all(row["status"] != "candidate" for row in evaluated["obligations"])
