from __future__ import annotations

from typing import Any

from src.domain.schemas import CandidateFinding, CritiquerOutput, ReviewTask
from src.orchestration.nodes.application.critiquer import make_general_critiquer_node
from src.orchestration.nodes.application.review_adjudicator import make_review_adjudicator_node


class _Raw:
    usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    response_metadata = None
    content = "raw"


class _SequencedLlm:
    def __init__(self, actions: list[CritiquerOutput]) -> None:
        self.actions = actions
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {"parsed": self.actions.pop(0), "raw": _Raw()}


def _candidate(
    candidate_id: str,
    *,
    content: str,
    failure_mode: str,
    file_path: str = "src/app.py",
    line_start: int = 1,
    line_end: int = 3,
) -> CandidateFinding:
    return CandidateFinding(
        candidate_id=candidate_id,
        patch_task_id="t1",
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        content=content,
        claim_type="defect",
        failure_mode=failure_mode,
        evidence_summary="local evidence",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )


def test_general_critiquer_runs_same_checker_negative_continuation(monkeypatch) -> None:
    first = _candidate("t1:c1", content="first issue", failure_mode="first failure")
    duplicate = _candidate("t1:c1-repeat", content="first issue", failure_mode="first failure")
    distinct = _candidate(
        "t1:c2",
        content="second issue",
        failure_mode="second failure",
        file_path="src/other.py",
        line_start=4,
        line_end=5,
    )
    fake = _SequencedLlm(
        [
            CritiquerOutput(summary="first pass", candidates=[first]),
            CritiquerOutput(summary="continuation", candidates=[duplicate, distinct]),
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.critiquer.Models.worker",
        lambda *_args, **_kwargs: fake,
    )
    task = ReviewTask(
        id="t1",
        title="Review app",
        description="Review app behavior",
        target_files=["src/app.py", "src/other.py"],
        specialty="logic",
    )
    state = {
        "current_task_id": "t1",
        "task_registry": {"t1": task},
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                            "direct_context": "def handle():\n    return 1\n\n\ndef other():\n    return 2\n",
                        "context_packet": {"sections": []},
                        "task_evidence": {
                            "file_contents": {
                                "src/app.py": "def handle():\n    return 1\n",
                                "src/other.py": "def other():\n    value = 1\n    if value:\n        return 2\n",
                            }
                        },
                    }
                }
            }
        },
    }

    out = make_general_critiquer_node(
        context_provider=object(),
        use_pipeline_cache=True,
    )(state)  # type: ignore[arg-type]

    assert [candidate.candidate_id for candidate in out["candidate_findings"]] == ["t1:c1", "t1:c2"]
    assert len(fake.prompts) == 2
    assert "Do not repeat" in fake.prompts[1]
    task_meta = out["metadata"]["general_critiquer"]["by_task"]["t1"]
    assert task_meta["continuation_source_by_candidate"] == {
        "t1:c2": "same_checker_negative_prompt"
    }
    assert task_meta["continuation_duplicate_candidate_ids"] == ["t1:c1-repeat"]

    adjudicated = make_review_adjudicator_node(use_llm=False)(
        {**state, "candidate_findings": out["candidate_findings"], "metadata": out["metadata"]}
    )
    lifecycle = adjudicated["metadata"]["review_adjudicator"]["candidate_lifecycle"]
    assert "t1:c2" in lifecycle
