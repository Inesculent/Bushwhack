from src.orchestration.nodes.verifier.result_judge import (
    classify_attempt_failure,
    missing_modules_from_attempts,
)
from src.orchestration.nodes.verifier.source_only import source_only_verify_candidate
from src.domain.verifier_schemas import VerifierAttemptRecord


def _task_evidence(file_contents: dict[str, str], *, complete: bool = True) -> dict:
    return {
        "file_contents": file_contents,
        "files_complete": {path: complete for path in file_contents},
    }


def test_source_only_detects_removed_import_still_used() -> None:
    state = {
        "git_diff": "\n".join(
            [
                "+++ b/pkg/mod.py",
                "-import time",
                "+import os",
            ]
        ),
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": _task_evidence(
                            {"pkg/mod.py": "def f():\n    return time.sleep(1)\n"}
                        )
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == "verified"
    assert "time" in rationale
    assert attempt is not None
    assert attempt.sandbox_mode == "source_only_static"


def test_classify_attempt_failure_extracts_module_not_found() -> None:
    attempt = VerifierAttemptRecord(
        attempt_number=1,
        exit_code=2,
        stdout="STATUS: HARNESS_ERROR | ModuleNotFoundError: No module named 'dotenv'",
        stderr="ModuleNotFoundError: No module named 'dotenv'",
    )

    assert classify_attempt_failure(attempt) == "module_not_found"
    assert missing_modules_from_attempts([attempt]) == ["dotenv"]


def test_source_only_detects_missing_return_fallthrough() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": _task_evidence(
                            {
                                "pkg/mod.py": (
                                    "def execute(mode):\n"
                                    "    if mode == 'A':\n"
                                    "        return (True,)\n"
                                )
                            }
                        )
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 1,
        "failure_mode": "execute has a missing return and can fall through to implicit None",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == "verified"
    assert "fall through" in rationale
    assert attempt is not None
    assert attempt.failure_class == "missing_return"


def test_source_only_detects_shape_cardinality_first_element_data_loss() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": _task_evidence(
                            {
                                "pkg/mod.py": (
                                    "def execute(records):\n"
                                    "    return ','.join([row[0] for row in records])\n"
                                )
                            }
                        )
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 1,
        "failure_mode": "Record serialization loses data because complete records should preserve all fields.",
        "evidence_summary": "The changed path keeps only one field from each record.",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == "verified"
    assert "element 0" in rationale
    assert attempt is not None


def test_source_only_does_not_treat_generic_all_as_shape_contract() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": _task_evidence(
                            {
                                "pkg/mod.py": (
                                    "def execute(items):\n"
                                    "    return items[0]\n"
                                )
                            }
                        )
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 1,
        "failure_mode": "All modes should return consistently.",
        "evidence_summary": "The mode contract is under review.",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == ""
    assert rationale == ""
    assert attempt is None


def test_source_only_detects_nested_item_projection_data_loss() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": _task_evidence(
                            {
                                "pkg/mod.py": (
                                    "def execute(items):\n"
                                    "    projected = [item[0] for item in items]\n"
                                    "    return projected\n"
                                )
                            }
                        )
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 1,
        "failure_mode": "Nested item data loss: complete structured items keep only the first element.",
        "evidence_summary": "The changed projection ignores later item fields.",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == "verified"
    assert "element 0" in rationale
    assert attempt is not None


def test_source_only_detects_optional_value_join_risk() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": _task_evidence(
                            {
                                "pkg/mod.py": (
                                    "def execute(rows, join_delimiter):\n"
                                    "    results = []\n"
                                    "    for row in rows:\n"
                                    "        results.append(row.get('name'))\n"
                                    "    return join_delimiter.join(results)\n"
                                )
                            }
                        )
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 1,
        "failure_mode": "Aggregation can crash when optional record fields produce None.",
        "evidence_summary": "Optional values are appended and joined without normalization.",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == "verified"
    assert "absent or non-string" in rationale
    assert attempt is not None


def test_source_only_abstains_on_incomplete_task_evidence_parse_error() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": _task_evidence(
                            {"pkg/mod.py": "def execute():\n    if True:\n"},
                            complete=False,
                        )
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 1,
        "failure_mode": "syntax parse error",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == ""
    assert "incomplete" in rationale
    assert attempt is None


def test_source_only_abstains_on_incomplete_task_evidence_missing_return() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": _task_evidence(
                            {
                                "pkg/mod.py": (
                                    "def execute(mode):\n"
                                    "    if mode == 'A':\n"
                                    "        return (True,)\n"
                                )
                            },
                            complete=False,
                        )
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 1,
        "failure_mode": "execute has a missing return and can fall through to implicit None",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == ""
    assert "incomplete" in rationale
    assert attempt is None
