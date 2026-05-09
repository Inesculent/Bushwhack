from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.data.research_pipeline.github_api import PullRequestContext
from src.domain.schemas import PreflightSummary, ReviewTask
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.reviewer_graph import _route_start as route_reviewer_start
from src.orchestration.reviewer_graph_basic import _route_start as route_basic_start
from src.reviewer_agent.harness.aacr import _invoke_for_pr


@dataclass
class FakeDumpable:
    payload: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return self.payload


def test_snapshot_resume_marks_docs_prebrief_done(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_reviewer(state: dict[str, Any]) -> dict[str, Any]:
        captured["state"] = state
        return state

    context = PullRequestContext(
        pr_url="https://github.com/comfyanonymous/ComfyUI/pull/7952",
        repo="comfyanonymous/ComfyUI",
        number=7952,
        title="Fix comfy node",
        body="",
        unified_diff="diff --git a/x.py b/x.py\n",
    )
    snapshot_data = {
        "snapshot_root": "C:/snapshots/example",
        "snapshot_id": "abc123def456",
        "repo_path": "https://github.com/comfyanonymous/ComfyUI",
        "graph_payload": {"nodes": [], "edges": []},
        "topology": FakeDumpable({"communities": []}),
        "community_summaries": [],
        "global_summary": "Existing snapshot summary.",
    }

    _invoke_for_pr(
        run_id="run1",
        pr_url=context.pr_url,
        context=context,
        repo_root=tmp_path,
        trace=False,
        started_at="2026-05-08T00:00:00+00:00",
        run_reviewer_fn=fake_run_reviewer,
        experiment_tag="test",
        snapshot_data=snapshot_data,
    )

    docs_meta = captured["state"]["metadata"]["docs_prebrief"]
    assert docs_meta["status"] == "skipped_snapshot_resume"
    assert captured["state"]["snapshot_root"] == "C:/snapshots/example"
    assert captured["state"]["repo_path"] == str(tmp_path.resolve())
    assert captured["state"]["metadata"]["review_repo_url"] == "https://github.com/comfyanonymous/ComfyUI"
    assert isinstance(captured["state"]["preflight_summary"], PreflightSummary)


def test_snapshot_resume_start_routes_directly_to_planner() -> None:
    state = {
        "run_id": "run1:repo__pr1_from_snapshot_abc123",
        "repo_path": "https://github.com/comfyanonymous/ComfyUI",
        "git_diff": "diff --git a/x.py b/x.py\n",
        "snapshot_root": "C:/snapshots/example",
        "preflight_summary": {"manifest_id": "snapshot_abc123"},
        "structural_graph_node_link": {"nodes": [], "edges": []},
        "metadata": {
            "docs_prebrief": {
                "status": "skipped_snapshot_resume",
                "reason": "snapshot_resume_uses_precomputed_context",
            },
        },
    }

    assert route_reviewer_start(state) == "review_planner"
    assert route_basic_start(state) == "review_planner"


def test_review_context_degrades_when_sandbox_startup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingSandbox:
        def __init__(self) -> None:
            raise RuntimeError("channel 3: open failed: connect failed: Connection refused")

    monkeypatch.setattr("src.orchestration.context.review_context.RepoSandbox", FailingSandbox)

    provider = LazyReviewContextProvider()
    context = provider.collect_for_task(
        {
            "run_id": "run1",
            "repo_path": "https://github.com/comfyanonymous/ComfyUI",
            "git_diff": "",
            "metadata": {"pr_number": 7952},
        },
        ReviewTask(
            id="review-general",
            title="General review",
            description="Review changed files",
            target_files=["x.py"],
            specialty="general",
        ),
    )

    assert context.explored_files == []
    assert context.warnings[:1] == [
        "sandbox_startup_failed:RuntimeError: channel 3: open failed: connect failed: Connection refused"
    ]
    assert "search_unavailable" in context.warnings
