from __future__ import annotations

from src.domain.schemas import AuditCoverageRecord, CandidateFinding, ReviewTask
from src.orchestration.routing.review_obligations import (
    derive_review_obligations,
    evaluate_review_obligations,
)


def _dimensions(obligations: list[dict[str, object]]) -> set[str]:
    return {str(row["dimension"]) for row in obligations}


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


def test_signature_obligation_requires_task_intent_and_code_evidence() -> None:
    evidence = {
        "file_contents": {
            "src/service.cpp": "\n".join(
                [
                    "#include \"service.h\"",
                    "enum class RotationMotion { Clockwise, CounterClockwise };",
                    "void DrawProjGroup::rotate(RotationMotion motion) {",
                    "    spin(motion);",
                    "}",
                ]
            )
        },
        "files_complete": {"src/service.cpp": True},
    }
    api_task = ReviewTask(
        id="general-api",
        title="API integration review",
        description="Audit signature compatibility, call sites, and typed public interfaces.",
        target_files=["src/service.cpp"],
        specialty="general",
    )
    unrelated_task = ReviewTask(
        id="general-style",
        title="General cleanup review",
        description="Review local readability only.",
        target_files=["src/service.cpp"],
        specialty="general",
    )

    assert "api/signature compatibility" in _dimensions(
        derive_review_obligations(api_task, evidence)
    )
    assert "api/signature compatibility" not in _dimensions(
        derive_review_obligations(unrelated_task, evidence)
    )


def test_generic_general_task_does_not_explode_into_new_dimensions() -> None:
    task = ReviewTask(
        id="general-task",
        title="General review",
        description="Review maintainability, error handling, tests, and integration consistency.",
        target_files=["src/app.py"],
        specialty="general",
    )
    evidence = {
        "file_contents": {
            "src/app.py": "\n".join(
                [
                    "import json",
                    "cache = {}",
                    "def handle(request):",
                    "    if request == 'ok':",
                    "        return json.dumps({'ok': True})",
                    "    return None",
                ]
            )
        },
        "files_complete": {"src/app.py": True},
    }

    dims = _dimensions(derive_review_obligations(task, evidence))
    assert "contract completeness" in dims
    assert "branch exhaustiveness" in dims
    assert {
        "api/signature compatibility",
        "dependency/import availability",
        "nullability/panic safety",
        "state/cache lifecycle",
        "protocol/output fidelity",
        "concurrency/shared-state safety",
        "security/input boundary",
        "repository convention contract",
        "public/user contract",
        "maintainability contract",
    }.isdisjoint(dims)


def test_functional_and_contract_obligations_are_task_conditioned() -> None:
    evidence = {
        "file_contents": {
            "pkg/node.py": "\n".join(
                [
                    "class CaseConverter:",
                    "    RETURN_TYPES = (\"STRING\",)",
                    "    INPUT_TYPES = {\"required\": {\"mode\": ([\"upper\", \"lower\"],)}}",
                    "    tooltip = \"Convert text\"",
                    "    # TODO: remove unused normalize_path helper after migration",
                    "    def execute(self, value):",
                    "        print(f\"mode={value}\")",
                    "        return (value.upper(),)",
                ]
            )
        },
        "files_complete": {"pkg/node.py": True},
    }
    convention_task = ReviewTask(
        id="general-convention",
        title="Repository convention contract",
        description="Audit documented framework syntax, user-visible messages, and unused code policy.",
        target_files=["pkg/node.py"],
        specialty="general",
    )
    narrow_task = ReviewTask(
        id="logic-branch",
        title="Branch exhaustiveness",
        description="Audit return paths only.",
        target_files=["pkg/node.py"],
        specialty="logic",
    )

    dims = _dimensions(derive_review_obligations(convention_task, evidence))
    assert {
        "repository convention contract",
        "public/user contract",
        "maintainability contract",
    } <= dims
    generic_rows = [
        row
        for row in derive_review_obligations(convention_task, evidence)
        if row["dimension"] in {"repository convention contract", "public/user contract", "maintainability contract"}
    ]
    assert {row["diff_signal_family"] for row in generic_rows} == {"contract_delta"}
    assert {row["issue_family"] for row in generic_rows} == {"contract_delta"}
    narrow_dims = _dimensions(derive_review_obligations(narrow_task, evidence))
    assert "repository convention contract" not in narrow_dims
    assert "public/user contract" not in narrow_dims
    assert "maintainability contract" not in narrow_dims


def test_broader_functional_obligations_are_evidence_gated() -> None:
    evidence = {
        "file_contents": {
            "src/handler.go": "\n".join(
                [
                    "import \"sync\"",
                    "var cache = map[string]string{}",
                    "var mu sync.Mutex",
                    "func Serve(userInput string) string {",
                    "    mu.Lock()",
                    "    defer mu.Unlock()",
                    "    if cache[userInput] != \"\" { return cache[userInput] }",
                    "    return fmt.Sprintf(\"status=%s\", userInput)",
                    "}",
                ]
            )
        },
        "files_complete": {"src/handler.go": True},
    }
    task = ReviewTask(
        id="functional-topics",
        title="Functional boundary review",
        description=(
            "Audit import availability, cache lifecycle, output format, concurrency shared state, "
            "and security validation for this handler."
        ),
        target_files=["src/handler.go"],
        specialty="security",
    )
    narrow_task = ReviewTask(
        id="format-only",
        title="Protocol output review",
        description="Audit exact output formatting for this handler.",
        target_files=["src/handler.go"],
        specialty="general",
    )

    dims = _dimensions(derive_review_obligations(task, evidence))
    assert {
        "dependency/import availability",
        "state/cache lifecycle",
        "protocol/output fidelity",
        "concurrency/shared-state safety",
        "security/input boundary",
    } <= dims
    narrow_dims = _dimensions(derive_review_obligations(narrow_task, evidence))
    assert "protocol/output fidelity" in narrow_dims
    assert "dependency/import availability" not in narrow_dims
    assert "concurrency/shared-state safety" not in narrow_dims
    assert "security/input boundary" not in narrow_dims


def test_new_candidate_dimensions_mark_matching_obligations() -> None:
    task = ReviewTask(
        id="logic-crash",
        title="Diff-local correctness",
        description="Audit null safety and crash behavior in the changed function.",
        target_files=["pkg/h.py"],
        specialty="logic",
    )
    evidence = {
        "file_contents": {
            "pkg/h.py": "def handle(value):\n    if value is None:\n        return None\n    return value.name\n"
        },
        "files_complete": {"pkg/h.py": True},
    }
    obligations = derive_review_obligations(task, evidence)
    assert "nullability/panic safety" in _dimensions(obligations)
    candidate = CandidateFinding(
        candidate_id="logic-crash-1",
        patch_task_id=task.id,
        file_path="pkg/h.py",
        line_start=1,
        line_end=4,
        content="The handler can crash on a None value after the new path.",
        claim_type="defect",
        failure_mode="Null pointer style crash on valid None input.",
        evidence_summary="The code returns None in one path and dereferences value.name in another.",
        recommendation="Preserve a single non-null contract or guard before dereference.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        behavioral_symptom="crash",
        root_operation="contract",
    )

    evaluated = evaluate_review_obligations(obligations, [candidate], [])
    statuses = {row["dimension"]: row["status"] for row in evaluated["obligations"]}
    assert statuses["nullability/panic safety"] == "candidate"


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


def test_obligations_prefer_task_named_class_when_whole_file_is_present() -> None:
    task = ReviewTask(
        id="logic-regex",
        title="RegexExtract review",
        description="Audit RegexExtract structured extraction behavior.",
        target_files=["pkg/nodes.py"],
        specialty="logic",
    )
    evidence = {
        "file_contents": {
            "pkg/nodes.py": "\n".join(
                [
                    "class StringConcatenate:",
                    "    def execute(self, a, b):",
                    "        return a + b",
                    "",
                    "class RegexExtract:",
                    "    def execute(self, mode, rows):",
                    "        if mode == 'All Matches':",
                    "            return [row[0] for row in rows]",
                    "        return '\\n'.join(rows)",
                ]
            )
        },
        "files_complete": {"pkg/nodes.py": True},
    }

    obligations = derive_review_obligations(task, evidence)
    assert {row["surface"] for row in obligations} == {"RegexExtract"}
    assert {row["dimension"] for row in obligations} >= {
        "branch exhaustiveness",
        "structured data preservation",
        "aggregation/serialization safety",
    }
