from src.domain.schemas import (
    CodeEntity,
    ReviewFinding,
    ReviewSurface,
    ReviewTask,
    StructuralTopologyCommunity,
    StructuralTopologySummary,
)
from src.orchestration.nodes.application.planner import (
    _amend_diff_narrowed_tasks,
    _baseline_diff_local_correctness_task,
    _chunk_logic_tasks_by_surface,
    _diff_signals_structured_extraction,
    _ensure_diff_local_correctness_task,
    _ensure_structured_extraction_logic_task,
    _is_duplicate_task,
    _normalize_tasks,
    _render_planner_prompt,
    _task_covers_structured_extraction,
    finalize_emitted_tasks,
    _sanitize_batched_logic_task_description,
    make_review_planner_node,
    validate_surface_bound_plan,
)
from src.orchestration.nodes.application.actor_critic_planner import make_plan_emit_node
from src.orchestration.context.surface_ledger import build_surface_ledger_from_diff, surface_ids_for_text
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


def test_baseline_diff_local_task_lists_multi_surface_inventory() -> None:
    diff = "\n".join(
        f"diff --git a/pkg/h{i}.py b/pkg/h{i}.py\n+++ b/pkg/h{i}.py\n+class H{i}:\n+    pass\n"
        for i in range(5)
    )
    state = {"git_diff": diff, "metadata": {"mental_model": {"diff_surface_inventory": [f"H{i}" for i in range(5)]}}}
    task = _baseline_diff_local_correctness_task(["pkg/h0.py"], state)
    assert "H0" in task.description
    assert "H4" in task.description
    assert "entry point" in task.description.lower()


def test_amend_diff_narrowed_tasks_expands_logic_scope_after_bootstrap() -> None:
    surfaces = [f"Node{i}" for i in range(6)]
    state = {
        "git_diff": "",
        "metadata": {
            "mental_model": {
                "bootstrap_completed": True,
                "diff_surface_inventory": surfaces,
            }
        },
    }
    narrow = ReviewTask(
        id="logic-diff-local-5-nodes",
        title="Diff-local correctness: 5 visible nodes",
        description="Focus only on the 322-line excerpt; do not infer behavior for unexposed nodes.",
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
    )
    out = _amend_diff_narrowed_tasks([narrow], state)
    assert "do not infer" not in out[0].description.lower()
    assert "Node0" in out[0].description
    assert "entry point" in out[0].description.lower()


def test_diff_signals_structured_extraction_from_findall_and_join() -> None:
    diff = "\n".join(
        [
            "diff --git a/pkg/h.py b/pkg/h.py",
            "+++ b/pkg/h.py",
            "+    rows = re.findall(pat, s)",
            "+    return ','.join(rows)",
        ]
    )
    assert _diff_signals_structured_extraction({"git_diff": diff}) is True


def test_mega_logic_checklist_does_not_block_structured_extraction_task() -> None:
    mega = ReviewTask(
        id="task-1",
        title="Diff-local correctness for all handlers",
        description=(
            "Audit all handlers for branch exhaustiveness and structured result paths "
            "and aggregation in the changed file."
        ),
        target_files=["pkg/h.py"],
        specialty="logic",
    )
    assert _task_covers_structured_extraction(mega) is False


def test_chunk_monolithic_logic_into_class_scoped_shards() -> None:
    surfaces = [f"Node{i}" for i in range(10)]
    state = {
        "git_diff": "diff --git a/pkg/h.py b/pkg/h.py\n+++ b/pkg/h.py\n+import re\n+re.findall(x)\n",
        "metadata": {"mental_model": {"diff_surface_inventory": surfaces}},
    }
    mega = ReviewTask(
        id="task-1",
        title="Diff-local correctness all nodes",
        description="Audit each of: " + ", ".join(surfaces) + ".",
        target_files=["pkg/h.py"],
        specialty="logic",
    )
    out = _chunk_logic_tasks_by_surface(
        [mega, ReviewTask(id="task-2", title="Security", description="ReDoS", target_files=["pkg/h.py"], specialty="security")],
        state,
    )
    logic = [t for t in out if t.specialty == "logic"]
    assert len(logic) >= 5
    assert all("do not review any other class" in t.description.lower() for t in logic)
    mentioned = {name for t in logic for name in surfaces if name in t.description}
    assert mentioned == set(surfaces)


def test_chunk_comfy_like_inventory_signal_tasks() -> None:
    surfaces = [
        "StringConcatenate",
        "StringSubstring",
        "StringLength",
        "CaseConverter",
        "StringTrim",
        "StringReplace",
        "StringContains",
        "StringCompare",
        "RegexMatch",
        "RegexExtract",
    ]
    diff = "\n".join(
        [
            "diff --git a/comfy_extras/nodes_string.py b/comfy_extras/nodes_string.py",
            "+++ b/comfy_extras/nodes_string.py",
            "+class StringCompare():",
            "+    elif mode == 'Equal':",
            "+        return a == b",
            "+    elif mode == 'Ends With':",
            "+        return a.endswith(b)",
            "+class RegexExtract():",
            "+    rows = re.findall(pat, s)",
            "+    return join_delimiter.join(rows)",
        ]
    )
    state = {"git_diff": diff, "metadata": {"mental_model": {"diff_surface_inventory": surfaces}}}
    mega = ReviewTask(
        id="task-1",
        title="Diff-local correctness",
        description="Diff-local correctness for all handlers: " + ", ".join(surfaces),
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
    )
    out = _chunk_logic_tasks_by_surface([mega], state)
    logic = [t for t in out if t.specialty == "logic"]
    assert len(logic) >= 4
    regex_tasks = [t for t in logic if "RegexExtract" in t.description]
    assert regex_tasks
    assert any("type-tracing" in t.description.lower() for t in regex_tasks)
    compare_tasks = [t for t in logic if "StringCompare" in t.description]
    assert compare_tasks
    assert any("branch-exhaustiveness" in t.description.lower() for t in compare_tasks)


def test_finalize_emitted_tasks_preserves_surface_ids_for_many_single_file_surfaces() -> None:
    surfaces = [f"Node{i}" for i in range(9)]
    diff = "\n".join(
        [
            "diff --git a/pkg/nodes.py b/pkg/nodes.py",
            "+++ b/pkg/nodes.py",
            "@@ -0,0 +1,36 @@",
            *[f"+class {name}:\n+    pass" for name in surfaces],
        ]
    )
    state = {"git_diff": diff}
    mega = ReviewTask(
        id="logic-all",
        title="Diff-local correctness for every node",
        description="Audit all changed nodes: " + ", ".join(surfaces),
        target_files=["pkg/nodes.py"],
        specialty="logic",
    )

    out = finalize_emitted_tasks([mega], state)

    logic = [task for task in out if task.specialty == "logic"]
    assert len(logic) >= 4
    owners: dict[str, str] = {}
    for task in logic:
        assert task.surface_ids
        for sid in task.surface_ids:
            assert sid not in owners
            owners[sid] = task.id
        names_in_description = [name for name in surfaces if name in task.description]
        assert len(names_in_description) < len(surfaces)
    assert len(owners) == len(build_surface_ledger_from_diff(diff))


def test_surface_plan_validation_rejects_non_changed_target_file() -> None:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def handle():\n"
        "+    return None\n"
    )
    task = ReviewTask(
        id="logic-handle",
        title="Diff-local correctness: handle",
        description="Audit handle only.",
        target_files=["tests/test_app.py"],
        specialty="logic",
    )

    diagnostics = validate_surface_bound_plan([task], {"git_diff": diff})

    assert diagnostics["ok"] is False
    assert diagnostics["invalid_target_files"] == [
        {"task_id": "logic-handle", "file_path": "tests/test_app.py"}
    ]


def test_surface_ledger_recalls_existing_entity_when_body_changes() -> None:
    diff = (
        "diff --git a/src/cache.py b/src/cache.py\n"
        "+++ b/src/cache.py\n"
        "@@ -12,7 +12,7 @@\n"
        " def _untouch(blocks):\n"
        "     for block in blocks:\n"
        "-        queue.append(block)\n"
        "+        queue.appendleft(block)\n"
    )
    entities = {
        "src/cache.py": [
            CodeEntity(
                name="_untouch",
                type="function",
                signature="def _untouch(blocks):",
                body="def _untouch(blocks):\n    for block in blocks:\n        queue.appendleft(block)\n",
                definition_line=10,
                definition_end_line=14,
            )
        ]
    }

    ledger = build_surface_ledger_from_diff(diff, entities_by_file=entities)

    untouch = next(surface for surface in ledger if surface.name == "_untouch")
    assert untouch.source == "ast_enclosing_diff_hunk"
    assert untouch.line_start == 10
    assert untouch.line_end == 14


def test_surface_ledger_does_not_assign_ambiguous_inventory_to_first_file() -> None:
    diff = (
        "diff --git a/pkg/a.py b/pkg/a.py\n"
        "+++ b/pkg/a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old_a()\n"
        "+new_a()\n"
        "diff --git a/pkg/b.py b/pkg/b.py\n"
        "+++ b/pkg/b.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old_b()\n"
        "+new_b()\n"
    )

    ledger = build_surface_ledger_from_diff(diff, inventory=["SharedName"])

    assert all(surface.name != "SharedName" for surface in ledger)


def test_surface_ids_for_text_ignores_negated_surface_mentions() -> None:
    surface = ReviewSurface(
        surface_id="surface:b",
        name="model_patcher.py",
        kind="file",
        file_path="pkg/model_patcher.py",
        confidence=0.95,
    )

    ids = surface_ids_for_text("Audit cache behavior without reviewing model_patcher.py.", [surface])

    assert ids == []


def test_surface_plan_validation_ignores_out_of_scope_file_surface_overlap() -> None:
    diff = (
        "diff --git a/pkg/a.py b/pkg/a.py\n"
        "+++ b/pkg/a.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def handle_a():\n"
        "+    return None\n"
        "diff --git a/pkg/b.py b/pkg/b.py\n"
        "+++ b/pkg/b.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def handle_b():\n"
        "+    return None\n"
    )
    file_surface = ReviewSurface(
        surface_id="surface:file-b",
        name="b.py",
        kind="file",
        file_path="pkg/b.py",
        confidence=0.95,
    )
    handle_a = ReviewSurface(
        surface_id="surface:handle-a",
        name="handle_a",
        kind="function",
        file_path="pkg/a.py",
        confidence=0.95,
    )
    handle_b = ReviewSurface(
        surface_id="surface:handle-b",
        name="handle_b",
        kind="function",
        file_path="pkg/b.py",
        confidence=0.95,
    )
    state = {
        "git_diff": diff,
        "metadata": {
            "mental_model": {
                "surface_ledger": [
                    file_surface.model_dump(),
                    handle_a.model_dump(),
                    handle_b.model_dump(),
                ]
            }
        },
    }
    first = ReviewTask(
        id="logic-a",
        title="Diff-local correctness in a.py",
        description="Audit a.py only, without reviewing b.py.",
        target_files=["pkg/a.py"],
        surface_ids=["surface:handle-a", "surface:file-b"],
        specialty="logic",
    )
    second = ReviewTask(
        id="logic-b",
        title="Diff-local correctness in b.py",
        description="Audit b.py.",
        target_files=["pkg/b.py"],
        surface_ids=["surface:file-b", "surface:handle-b"],
        specialty="logic",
    )

    diagnostics = validate_surface_bound_plan([first, second], state)

    assert diagnostics["ok"] is True
    assert diagnostics["overlapping_tasks"] == []


def test_surface_plan_validation_still_rejects_same_symbol_logic_overlap() -> None:
    diff = (
        "diff --git a/pkg/a.py b/pkg/a.py\n"
        "+++ b/pkg/a.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def handle():\n"
        "+    return None\n"
    )
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="pkg/a.py",
        confidence=0.95,
    )
    state = {
        "git_diff": diff,
        "metadata": {"mental_model": {"surface_ledger": [surface.model_dump()]}},
    }
    first = ReviewTask(
        id="logic-a",
        title="Diff-local correctness: handle branches",
        description="Audit handle branch behavior.",
        target_files=["pkg/a.py"],
        surface_ids=["surface:handle"],
        specialty="logic",
    )
    second = ReviewTask(
        id="logic-b",
        title="Diff-local correctness: handle returns",
        description="Audit handle return behavior.",
        target_files=["pkg/a.py"],
        surface_ids=["surface:handle"],
        specialty="logic",
    )

    diagnostics = validate_surface_bound_plan([first, second], state)

    assert diagnostics["ok"] is False
    assert diagnostics["overlapping_tasks"] == [
        {"surface_id": "surface:handle", "task_ids": ["logic-a", "logic-b"]}
    ]


def test_plan_emit_emits_surface_valid_plan_when_critic_misaligned_after_budget() -> None:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def handle():\n"
        "+    return None\n"
    )
    task = ReviewTask(
        id="logic-handle",
        title="Diff-local correctness: handle",
        description="Audit handle only.",
        target_files=["src/app.py"],
        specialty="logic",
    )
    state = {
        "git_diff": diff,
        "metadata": {
            "actor_critic_planner": {
                "draft_tasks": [task.model_dump(mode="json")],
                "revision_count": 99,
                "aligned": False,
                "last_critique": {"gaps": "missing boundaries"},
            },
            "mental_model": {"coupled_loop": {"cycles": 99}},
        },
    }

    out = make_plan_emit_node()(state)

    assert out["next_step"] == "review"
    assert out["metadata"]["actor_critic_review"]["emitted_after_budget"] is True
    assert "plan_critic_misaligned_after_budget" in out["metadata"]["review_planner"]["warnings"]
    assert [task_id for task_id in out["task_registry"] if task_id != out["root_task_id"]] == [
        "logic-handle"
    ]


def test_plan_emit_still_blocks_surface_invalid_plan_after_budget() -> None:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def handle():\n"
        "+    return None\n"
    )
    task = ReviewTask(
        id="logic-handle",
        title="Diff-local correctness: handle",
        description="Audit handle only.",
        target_files=["tests/test_app.py"],
        specialty="logic",
    )
    state = {
        "git_diff": diff,
        "metadata": {
            "actor_critic_planner": {
                "draft_tasks": [task.model_dump(mode="json")],
                "revision_count": 99,
                "aligned": False,
                "last_critique": {"gaps": "missing boundaries"},
            },
            "mental_model": {"coupled_loop": {"cycles": 99}},
        },
    }

    out = make_plan_emit_node()(state)

    assert out["next_step"] == "blocked"
    assert out["metadata"]["review_planner"]["blocked"] is True
    assert out["metadata"]["review_planner"]["plan_validation"]["blocked_reason"] == (
        "surface_plan_validation_failed"
    )
    assert [task_id for task_id in out["task_registry"] if task_id != out["root_task_id"]] == []


def test_plan_emit_allows_aligned_surface_bound_plan() -> None:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def handle():\n"
        "+    return None\n"
    )
    task = ReviewTask(
        id="logic-handle",
        title="Diff-local correctness: handle",
        description="Audit handle only.",
        target_files=["src/app.py"],
        specialty="logic",
    )
    state = {
        "git_diff": diff,
        "metadata": {
            "actor_critic_planner": {
                "draft_tasks": [task.model_dump(mode="json")],
                "revision_count": 0,
                "aligned": True,
            }
        },
    }

    out = make_plan_emit_node()(state)

    assert out["next_step"] == "review"
    leaf_ids = [task_id for task_id in out["task_registry"] if task_id != out["root_task_id"]]
    assert leaf_ids == ["logic-handle"]
    assert out["metadata"]["review_planner"]["plan_validation"]["ok"] is True


def test_dedupe_allows_same_file_different_class_scopes() -> None:
    left = ReviewTask(
        id="a",
        title="RegexExtract — structured extraction",
        description="Type-tracing on RegexExtract only. Do not review any other class.",
        target_files=["nodes_string.py"],
        specialty="logic",
    )
    right = ReviewTask(
        id="b",
        title="StringCompare — branch exhaustiveness",
        description="Branch checks on StringCompare only. Do not review any other class.",
        target_files=["nodes_string.py"],
        specialty="logic",
    )
    assert _is_duplicate_task(right, [left]) is False


def test_sanitize_batched_logic_strips_cross_surface_boilerplate() -> None:
    inventory = [
        "StringConcatenate",
        "StringSubstring",
        "StringLength",
        "CaseConverter",
        "StringTrim",
        "StringReplace",
        "StringContains",
        "StringCompare",
        "RegexMatch",
        "RegexExtract",
    ]
    state = {
        "git_diff": "diff --git a/pkg/h.py b/pkg/h.py\n+++ b/pkg/h.py\n+class StringCompare():\n+    pass\n",
        "metadata": {"mental_model": {"diff_surface_inventory": inventory}},
    }
    suffix = (
        " Audit every changed entry point in: "
        + ", ".join(inventory)
        + ". For each: branch exhaustiveness on mode/discriminant inputs, consistent return on "
        "all paths, correct indexing into structured results (e.g. regex tuples, capture groups), "
        "and safe aggregation before return."
    )
    task = ReviewTask(
        id="logic_1a",
        title="StringConcatenate, StringSubstring, StringLength, CaseConverter, StringTrim: diff-local",
        description="Audit five nodes." + suffix,
        target_files=["pkg/h.py", "pkg/other.py"],
        specialty="logic",
    )
    cleaned = _sanitize_batched_logic_task_description(task, state, inventory)
    assert "Audit every changed entry point in:" not in cleaned
    assert "do not review any other class" in cleaned.lower()


def test_finalize_emitted_tasks_injects_structured_logic_task() -> None:
    diff = "diff --git a/pkg/h.py b/pkg/h.py\n+++ b/pkg/h.py\n+    return ','.join(re.findall(p, s))\n"
    state = {"git_diff": diff}
    tasks = [
        ReviewTask(
            id="task-1",
            title="Diff-local correctness",
            description="Diff-local correctness in changed hunks.",
            target_files=["pkg/h.py"],
            specialty="logic",
        ),
    ]
    out = finalize_emitted_tasks(tasks, state)
    logic = [t for t in out if t.specialty == "logic"]
    assert any(_task_covers_structured_extraction(t) for t in logic)


def test_broad_logic_structured_title_does_not_block_dedicated_task() -> None:
    diff = "diff --git a/pkg/h.py b/pkg/h.py\n+++ b/pkg/h.py\n+    return ','.join(re.findall(p, s))\n"
    state = {"git_diff": diff}
    inventory = ["HandlerA", "HandlerB"]
    state["metadata"] = {"mental_model": {"diff_surface_inventory": inventory}}
    tasks = [
        ReviewTask(
            id="logic_3",
            title="HandlerA, HandlerB - structured extraction and aggregation",
            description=(
                "Audit HandlerA and HandlerB for structured result handling. "
                "Verify index/slot selection and join paths."
            ),
            target_files=["pkg/h.py"],
            specialty="logic",
        ),
    ]
    out = _ensure_structured_extraction_logic_task(tasks, state)
    assert any(t.id == "review-logic-structured-extraction" for t in out)


def test_ensure_structured_extraction_logic_task_injected_when_signals_present() -> None:
    diff = "diff --git a/pkg/h.py b/pkg/h.py\n+++ b/pkg/h.py\n+    return ','.join(re.findall(p, s))\n"
    state = {"git_diff": diff}
    tasks = [
        ReviewTask(
            id="review-logic-diff-local",
            title="Diff-local correctness",
            description="Diff-local correctness: control flow in changed hunks.",
            target_files=["pkg/h.py"],
            specialty="logic",
        ),
        ReviewTask(
            id="review-security",
            title="Security",
            description="Injection and unsafe patterns.",
            target_files=["pkg/h.py"],
            specialty="security",
        ),
    ]
    out = _ensure_structured_extraction_logic_task(tasks, state)
    logic = [t for t in out if t.specialty == "logic"]
    assert len(logic) == 2
    assert any("structured" in f"{t.title} {t.description}".lower() for t in logic)


def test_ensure_diff_local_not_skipped_when_only_structured_scoped_task() -> None:
    inventory = [f"Node{i}" for i in range(10)]
    diff = "diff --git a/pkg/h.py b/pkg/h.py\n+++ b/pkg/h.py\n+import re\n+class Node0():\n+    pass\n"
    state = {
        "git_diff": diff,
        "metadata": {"mental_model": {"diff_surface_inventory": inventory}},
    }
    tasks = [
        ReviewTask(
            id="review-logic-structured-extraction",
            title="Structured extraction and aggregation",
            description=(
                "Audit structured extraction and aggregation in changed handlers. "
                "Do not review any other class in the target file."
            ),
            target_files=["pkg/h.py"],
            specialty="logic",
        ),
    ]
    out = _ensure_diff_local_correctness_task(tasks, state)
    assert any("diff-local" in f"{t.title} {t.description}".lower() for t in out)


def test_ensure_diff_local_correctness_injects_when_llm_plan_omits_it():
    state = {
        "run_id": "test",
        "repo_path": "/tmp/repo",
        "git_diff": "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n",
    }
    tasks = [
        ReviewTask(
            id="review-security",
            title="Auth chain review",
            description="Verify authorization decorators and caller permission checks across the repo.",
            target_files=["src/app.py"],
            specialty="security",
        ),
        ReviewTask(
            id="review-logic-callers",
            title="Caller contract review",
            description="Trace all callers of changed symbols and confirm middleware behavior.",
            target_files=["src/app.py"],
            specialty="logic",
        ),
    ]
    out = _ensure_diff_local_correctness_task(tasks, state)
    assert any("diff-local" in f"{t.title} {t.description}".lower() for t in out)
    assert len(out) == 3


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
    logic_tasks = [t for t in leaf_tasks if t.specialty == "logic"]
    assert any("diff-local" in f"{t.title} {t.description}".lower() for t in logic_tasks)
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
    assert "review topics as planning lenses" in prompt
    assert "not as a checklist that must produce one task per topic" in prompt
    assert "Create a topic-specific task only when" in prompt


def test_review_planner_flattens_nested_llm_task_output():
    nested = [
        ReviewTask(
            id="container",
            title="Container",
            description="LLM wrapper task that should not be executed.",
            target_files=["src/app.py"],
            specialty="general",
            subtasks=[
                ReviewTask(
                    id="review-security",
                    title="Security",
                    description="Security leaf.",
                    target_files=[],
                    specialty="security",
                ),
                ReviewTask(
                    id="logic-container",
                    title="Logic container",
                    description="Nested wrapper.",
                    target_files=["src/app.py"],
                    specialty="logic",
                    subtasks=[
                        ReviewTask(
                            id="review-logic",
                            title="Diff-local correctness",
                            description="Diff-local correctness leaf for changed hunks.",
                            target_files=[],
                            specialty="logic",
                        )
                    ],
                ),
            ],
        )
    ]

    normalized = _normalize_tasks(
        nested,
        {
            "run_id": "test",
            "repo_path": "/tmp/repo",
            "git_diff": "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n",
        },
    )

    assert [task.id for task in normalized] == ["review-security", "review-logic"]
    assert [task.specialty for task in normalized] == ["security", "logic"]
    assert all(task.subtasks == [] for task in normalized)
    assert all(task.target_files == ["src/app.py"] for task in normalized)


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
