from src.domain.verifier_schemas import VerifierAttemptRecord
from src.orchestration.nodes.verifier.result_judge import (
    classify_attempt_failure,
    missing_modules_from_attempts,
)
from src.orchestration.nodes.verifier.source_only import (
    extract_source_facts_for_candidate,
    source_only_verify_candidate,
)


def _task_evidence(file_contents: dict[str, str], *, complete: bool = True) -> dict:
    return {
        "file_contents": file_contents,
        "files_complete": {path: complete for path in file_contents},
    }


def _state(file_contents: dict[str, str], *, complete: bool = True, git_diff: str = "") -> dict:
    return {
        "git_diff": git_diff,
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {"task_evidence": _task_evidence(file_contents, complete=complete)}
                }
            }
        },
    }


def _candidate(**updates) -> dict:
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 1,
    }
    candidate.update(updates)
    return candidate


def _fact_kinds(state: dict, candidate: dict) -> set[str]:
    return {fact.fact_kind for fact in extract_source_facts_for_candidate(state, candidate)}


def test_source_only_extracts_removed_import_still_used_fact() -> None:
    state = _state(
        {"pkg/mod.py": "def f():\n    return time.sleep(1)\n"},
        git_diff="\n".join(["+++ b/pkg/mod.py", "-import time", "+import os"]),
    )

    facts = extract_source_facts_for_candidate(state, _candidate())

    assert {fact.fact_kind for fact in facts} == {"removed_import_still_used"}
    assert "time" in facts[0].summary


def test_classify_attempt_failure_extracts_module_not_found() -> None:
    attempt = VerifierAttemptRecord(
        attempt_number=1,
        exit_code=2,
        stdout="STATUS: HARNESS_ERROR | ModuleNotFoundError: No module named 'dotenv'",
        stderr="ModuleNotFoundError: No module named 'dotenv'",
    )

    assert classify_attempt_failure(attempt) == "module_not_found"
    assert missing_modules_from_attempts([attempt]) == ["dotenv"]


def test_source_only_extracts_fallthrough_projection_and_join_facts() -> None:
    state = _state(
        {
            "pkg/mod.py": (
                "def execute(mode, rows):\n"
                "    if mode == 'A':\n"
                "        return ','.join([row[0] for row in rows])\n"
            )
        }
    )

    kinds = _fact_kinds(state, _candidate())

    assert "reachable_fallthrough" in kinds
    assert "first_slot_projection" in kinds
    assert "join_aggregation" in kinds


def test_source_only_facts_are_not_verifier_verdicts() -> None:
    state = _state({"pkg/mod.py": "def execute(items):\n    return items[0]\n"})

    verdict, rationale, attempt = source_only_verify_candidate(state, _candidate())

    assert verdict == ""
    assert "selecting element 0" in rationale
    assert attempt is None


def test_source_only_extracts_syntax_fact_from_complete_evidence() -> None:
    state = _state({"pkg/mod.py": "def execute():\n    if True:\n"}, complete=True)

    facts = extract_source_facts_for_candidate(state, _candidate())

    assert {fact.fact_kind for fact in facts} == {"syntax_error"}


def test_source_only_ignores_incomplete_unparseable_evidence() -> None:
    state = _state({"pkg/mod.py": "def execute():\n    if True:\n"}, complete=False)

    assert extract_source_facts_for_candidate(state, _candidate()) == []
