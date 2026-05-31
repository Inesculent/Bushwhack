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
