"""Task-scoped evidence bundles: complete files and symbol units."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.domain.schemas import CodeEntity, ReviewTask
from src.domain.state import GraphState
from src.orchestration.context import task_evidence as task_evidence_module
from src.orchestration.context.context_packets import build_critique_probe_packet, enforce_packet_budget
from src.orchestration.context.task_evidence import (
    _pack_units,
    build_task_evidence,
    code_slice_from_task_evidence,
    diff_hunk_for_file,
    EvidenceUnit,
)
from src.orchestration.nodes.application.worker import ReviewTaskContext


def _state(**overrides: object) -> GraphState:
    base: GraphState = {
        "run_id": "t",
        "repo_path": "/repo",
        "git_diff": (
            "diff --git a/pkg/foo.py b/pkg/foo.py\n"
            "+++ b/pkg/foo.py\n"
            "@@ -1,3 +1,5 @@\n"
            "+def execute():\n"
            "+    return None\n"
        ),
        "user_goals": "",
        "metadata": {},
    }
    base.update(overrides)  # type: ignore[typeddict-unknown-key]
    return base


def test_pack_units_never_splits_inside_unit() -> None:
    big = EvidenceUnit(0, "big", "x" * 5000, "file")
    small = EvidenceUnit(1, "small", "ok", "symbol")
    included, chop = _pack_units([big, small], 100)
    assert len(included) == 1
    assert included[0].label == "small"
    assert chop is True


def test_single_file_complete_in_bundle() -> None:
    body = "".join(f"line {i}\n" for i in range(300))
    provider = MagicMock()
    provider.read_full_file.return_value = body
    task = ReviewTask(
        id="t1",
        title="String nodes",
        description="Review RegexExtract and StringCompare execute methods",
        target_files=["comfy_extras/nodes_string.py"],
    )
    ctx = ReviewTaskContext(
        file_snippets={"comfy_extras/nodes_string.py": body},
        explored_files=["comfy_extras/nodes_string.py"],
    )
    bundle = build_task_evidence(_state(), task, provider, ctx)
    assert bundle.files_complete.get("comfy_extras/nodes_string.py") is True
    assert "line 299" in bundle.rendered
    assert bundle.byte_chop is False


def test_code_slice_from_stored_file_contents() -> None:
    stored = {
        "file_contents": {
            "a.py": "".join(f"{i}\n" for i in range(1, 101)),
        }
    }
    slice_text = code_slice_from_task_evidence(stored, "a.py", 50, 55, padding=5)
    assert "45" in slice_text
    assert "60" in slice_text


def test_build_task_evidence_excludes_diff_from_rendered() -> None:
    body = "def execute():\n    return None\n"
    provider = MagicMock()
    provider.read_full_file.return_value = body
    task = ReviewTask(
        id="t1",
        title="X",
        description="execute method",
        target_files=["pkg/foo.py"],
    )
    ctx = ReviewTaskContext(
        file_snippets={"pkg/foo.py": body},
        entities_by_file={
            "pkg/foo.py": [
                CodeEntity(
                    name="execute",
                    type="function",
                    signature="def execute()",
                    body=body,
                    definition_line=1,
                )
            ]
        },
    )
    bundle = build_task_evidence(_state(), task, provider, ctx)
    assert "diff pkg/foo.py" not in bundle.rendered
    assert "execute" in bundle.rendered or "complete file" in bundle.rendered


def test_diff_hunk_for_file_extracts_path() -> None:
    diff = _state()["git_diff"]
    hunk = diff_hunk_for_file(diff, "pkg/foo.py", max_chars=8000)
    assert "+++ b/pkg/foo.py" in hunk
    assert "execute" in hunk


def test_class_scoped_task_uses_file_slice_not_truncated_ast_body() -> None:
    file_body = "\n".join(
        [
            "class StringCompare():",
            "    def execute(self, string_a, string_b, mode, case_sensitive, **kwargs):",
            "        if mode == 'Equal':",
            "            return string_a == string_b,",
            "        elif mode == 'Ends With':",
            "            return string_a.endswith(string_b),",
            "",
            "class RegexMatch():",
            "    pass",
        ]
    )
    truncated_ast_body = (
        "class StringCompare():\n"
        "    def execute(self, string_a, string_b, mode, case_sensitive, **kwargs):\n"
        "        elif mode == 'Ends With':\n"
        "            a.endswith("
    )
    provider = MagicMock()
    provider.read_full_file.return_value = file_body
    task = ReviewTask(
        id="review-logic-StringCompare",
        title="StringCompare.execute — branch exhaustiveness",
        description=(
            "Branch-exhaustiveness on StringCompare.execute() only. "
            "Do not review any other class in the target file."
        ),
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
    )
    state = {
        "run_id": "t",
        "git_diff": "diff --git a/comfy_extras/nodes_string.py b/comfy_extras/nodes_string.py\n+++ b/comfy_extras/nodes_string.py\n+class StringCompare\n",
        "metadata": {"mental_model": {"diff_surface_inventory": ["StringCompare", "RegexMatch"]}},
    }
    ctx = ReviewTaskContext(
        file_snippets={"comfy_extras/nodes_string.py": file_body},
        entities_by_file={
            "comfy_extras/nodes_string.py": [
                CodeEntity(
                    name="StringCompare",
                    type="class",
                    signature="class StringCompare",
                    body=truncated_ast_body,
                    definition_line=1,
                ),
            ]
        },
    )
    bundle = build_task_evidence(state, task, provider, ctx)
    assert "return string_a.endswith(string_b)," in bundle.rendered
    assert truncated_ast_body.strip() not in bundle.rendered
    assert bundle.files_complete.get("comfy_extras/nodes_string.py") is False
    stored = bundle.to_storage_dict()
    rendered_units = stored["rendered_units"]["comfy_extras/nodes_string.py"]
    assert "return string_a.endswith(string_b)," in rendered_units
    assert stored["rendered"] == bundle.rendered


def test_class_slice_fallback_recovers_when_primary_range_lookup_fails(monkeypatch) -> None:
    file_body = "\n".join(
        [
            "class StringCompare():",
            "    def execute(self, mode):",
            "        if mode == 'Equal':",
            "            return True,",
            "        elif mode == 'Ends With':",
            "            return False,",
        ]
    )
    provider = MagicMock()
    provider.read_full_file.return_value = file_body
    task = ReviewTask(
        id="review-logic-StringCompare",
        title="StringCompare.execute branch exhaustiveness",
        description=(
            "Branch-exhaustiveness on StringCompare.execute() only. "
            "Do not review any other class in the target file."
        ),
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
    )
    ctx = ReviewTaskContext(file_snippets={"comfy_extras/nodes_string.py": file_body})
    monkeypatch.setattr(task_evidence_module, "class_line_range_with_tail", lambda *args, **kwargs: None)

    bundle = build_task_evidence(
        {
            "run_id": "t",
            "git_diff": "",
            "metadata": {"mental_model": {"diff_surface_inventory": ["StringCompare"]}},
        },
        task,
        provider,
        ctx,
    )

    assert "return False," in bundle.rendered
    assert not any("evidence_class_range_missing" in warning for warning in bundle.warnings)
    assert any("evidence_class_range_recovered" in warning for warning in bundle.warnings)


def test_execute_on_changed_lines_priority_zero() -> None:
    core = (
        "class RegexExtract:\n"
        "    def execute(self, mode, pattern):\n"
        "        if mode == 'all':\n"
        "            return []\n"
        "        return None\n"
    )
    body = core + "\n" + ("# pad\n" * 8000)
    provider = MagicMock()
    provider.read_full_file.return_value = body
    task = ReviewTask(
        id="t1",
        title="Regex",
        description="Check regex node",
        target_files=["pkg/foo.py"],
    )
    ctx = ReviewTaskContext(
        file_snippets={"pkg/foo.py": body},
        entities_by_file={
            "pkg/foo.py": [
                CodeEntity(
                    name="RegexExtract",
                    type="class",
                    signature="class RegexExtract",
                    body=core,
                    definition_line=1,
                ),
                CodeEntity(
                    name="execute",
                    type="function",
                    signature="def execute",
                    body="    def execute(self, mode, pattern):\n        if mode == 'all':\n            return []\n        return None",
                    definition_line=2,
                ),
                CodeEntity(
                    name="helper_unused",
                    type="function",
                    signature="def helper_unused",
                    body="def helper_unused():\n    pass",
                    definition_line=20,
                ),
            ]
        },
    )
    bundle = build_task_evidence(_state(), task, provider, ctx)
    assert "execute" in bundle.rendered
    assert "helper_unused" not in bundle.rendered


def test_probe_packet_uses_task_evidence_not_raw_render() -> None:
    body = "def normalize_path():\n    pass\n\n" + ("# filler\n" * 50)
    task = ReviewTask(
        id="t2",
        title="Unused",
        description="Check normalize_path",
        target_files=["nodes_string.py"],
    )
    ctx = ReviewTaskContext(file_snippets={"nodes_string.py": body})
    packet = build_critique_probe_packet(
        _state(),
        task,
        ctx,
        code_evidence="--- nodes_string.py (complete file) ---\n" + body,
        evidence_metadata={"files_complete": {"nodes_string.py": True}, "byte_chop": False},
    )
    enforced = enforce_packet_budget(packet)
    code = next(s.content for s in enforced.sections if s.key == "code_evidence")
    assert "normalize_path" in code
    assert "complete file" in code
