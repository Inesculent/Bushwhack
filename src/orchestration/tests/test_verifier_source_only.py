from src.orchestration.nodes.verifier.result_judge import (
    classify_attempt_failure,
    missing_modules_from_attempts,
)
from src.orchestration.nodes.verifier.source_only import source_only_verify_candidate
from src.domain.verifier_schemas import VerifierAttemptRecord


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
                        "task_evidence": {
                            "file_contents": {
                                "pkg/mod.py": "def f():\n    return time.sleep(1)\n"
                            }
                        }
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
                        "task_evidence": {
                            "file_contents": {
                                "pkg/mod.py": (
                                    "def execute(mode):\n"
                                    "    if mode == 'A':\n"
                                    "        return (True,)\n"
                                )
                            }
                        }
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


def test_source_only_detects_regex_all_matches_data_loss() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": {
                            "file_contents": {
                                "pkg/mod.py": (
                                    "import re\n"
                                    "def execute(pattern, text):\n"
                                    "    match = re.search(pattern, text)\n"
                                    "    return match.group(1) if match else ''\n"
                                )
                            }
                        }
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 2,
        "failure_mode": "Regex extraction loses all matches because it returns only the first match.",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == "verified"
    assert "re.search" in rationale
    assert attempt is not None


def test_source_only_detects_regex_findall_tuple_field_data_loss() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": {
                            "file_contents": {
                                "pkg/mod.py": (
                                    "import re\n"
                                    "def execute(pattern, text):\n"
                                    "    matches = re.findall(pattern, text)\n"
                                    "    return ','.join([m[0] for m in matches])\n"
                                )
                            }
                        }
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 2,
        "failure_mode": "Regex all matches data loss: tuple results keep only the first group.",
        "evidence_summary": "The all matches branch ignores later tuple fields.",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == "verified"
    assert "tuple element 0" in rationale
    assert attempt is not None


def test_source_only_detects_regex_all_groups_join_none_risk() -> None:
    state = {
        "git_diff": "",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": {
                            "file_contents": {
                                "pkg/mod.py": (
                                    "import re\n"
                                    "def execute(pattern, text, group_index, join_delimiter):\n"
                                    "    results = []\n"
                                    "    for match in re.finditer(pattern, text):\n"
                                    "        results.append(match.group(group_index))\n"
                                    "    return join_delimiter.join(results)\n"
                                )
                            }
                        }
                    }
                }
            }
        },
    }
    candidate = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "pkg/mod.py",
        "line_start": 2,
        "failure_mode": "Regex all groups can crash or lose output when optional groups produce None.",
        "evidence_summary": "All groups are appended and joined without filtering None.",
    }

    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)

    assert verdict == "verified"
    assert "without filtering possible None" in rationale
    assert attempt is not None
