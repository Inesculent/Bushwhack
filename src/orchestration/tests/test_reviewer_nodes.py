from src.domain.schemas import ReviewFinding, ReviewTask, StructuralTopologyCommunity, StructuralTopologySummary
from src.orchestration.nodes.application.planner import _render_planner_prompt, make_review_planner_node
from src.orchestration.nodes.application.synthesizer import synthesizer_node
from src.orchestration.nodes.application.worker import (
    ReviewTaskContext,
    make_specialist_worker_node,
)


class FakeContextProvider:
    def collect_for_task(self, state, task):
        return ReviewTaskContext(
            explored_files=task.target_files,
            file_snippets={path: "def changed():\n    return True\n" for path in task.target_files},
        )


def test_review_planner_deterministic_fallback_creates_parallel_tasks():
    node = make_review_planner_node(use_llm=False)
    result = node(
        {
            "run_id": "test",
            "repo_path": "/tmp/repo",
            "git_diff": "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n",
            "global_insights": [],
            "findings": [],
            "node_history": [],
        }
    )

    registry = result["task_registry"]
    leaf_tasks = [task for task_id, task in registry.items() if task_id != result["root_task_id"]]

    assert {task.specialty for task in leaf_tasks} == {
        "security",
        "logic",
        "performance",
        "general",
    }
    assert all(task.target_files == ["src/app.py"] for task in leaf_tasks)
    assert result["next_step"] == "review"


def test_review_planner_prompt_uses_structural_routing_hints():
    topology = StructuralTopologySummary(
        algorithm="test",
        community_count=1,
        communities=[
            StructuralTopologyCommunity(
                community_id=1,
                node_ids=[f"node-{idx}" for idx in range(1000)],
                cohesion=0.8,
                file_count=10,
                symbol_count=900,
            )
        ],
        node_to_community={f"node-{idx}": 1 for idx in range(1000)},
        splits_applied=0,
        config={},
    )
    graph_payload = {
        "nodes": [
            {"id": "file:src/app.py", "node_type": "file", "file_path": "src/app.py"},
            {"id": "file:src/caller.py", "node_type": "file", "file_path": "src/caller.py"},
            {
                "id": "symbol:abc:changed",
                "node_type": "symbol",
                "file_path": "src/app.py",
                "symbol_name": "changed",
            },
        ],
        "edges": [
            {"source": "file:src/app.py", "target": "symbol:abc:changed", "edge_type": "defines"},
            {"source": "file:src/caller.py", "target": "file:src/app.py", "edge_type": "references"},
        ],
    }

    prompt = _render_planner_prompt(
        {
            "run_id": "test",
            "repo_path": "/tmp/repo",
            "git_diff": "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n",
            "structural_topology": topology,
            "structural_graph_node_link": graph_payload,
            "global_insights": [],
        }
    )

    assert "Structural Routing Hints" in prompt
    assert "changed_file_hints" in prompt
    assert "src/caller.py" in prompt
    assert "changed" in prompt
    assert "node_to_community" not in prompt
    assert "node-999" not in prompt


def test_specialist_worker_marks_task_complete_without_llm():
    task = ReviewTask(
        id="review-security",
        title="Security review",
        description="Review security risks.",
        specialty="security",
        target_files=["src/app.py"],
    )
    node = make_specialist_worker_node(
        "security",
        context_provider=FakeContextProvider(),
        use_llm=False,
    )

    result = node(
        {
            "run_id": "test",
            "repo_path": "/tmp/repo",
            "git_diff": "",
            "current_task_id": task.id,
            "task_registry": {task.id: task},
            "task_status_by_id": {task.id: "pending"},
            "findings": [],
            "reviewer_worker_reports": [],
            "node_history": [],
        }
    )

    assert result["task_status_by_id"] == {task.id: "completed"}
    assert result["reviewer_worker_reports"][0].explored_files == ["src/app.py"]


def test_synthesizer_deduplicates_final_findings():
    finding = ReviewFinding(
        id="review-logic:1",
        file_path="src/app.py",
        line_start=10,
        line_end=12,
        content="Potential regression in changed control flow.",
        severity="medium",
        feedback_type="defect_detection",
    )

    result = synthesizer_node(
        {
            "run_id": "test",
            "repo_path": "/tmp/repo",
            "git_diff": "",
            "findings": [finding, finding.model_copy(update={"id": "review-general:1"})],
            "reviewer_worker_reports": [],
            "node_history": [],
        }
    )

    assert len(result["final_findings"]) == 1
    assert result["metadata"]["review_synthesizer"]["raw_finding_count"] == 2
    assert result["metadata"]["review_synthesizer"]["final_finding_count"] == 1
