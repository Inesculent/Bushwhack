"""Task-scoped evidence bundles: complete files and symbol units."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.domain.schemas import CodeEntity, ReviewTask
from src.domain.state import GraphState
from src.orchestration.context import task_evidence as task_evidence_module
from src.orchestration.context.context_packets import (
    build_critique_probe_packet,
    build_critiquer_packet,
    enforce_packet_budget,
)
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


def test_class_scoped_task_includes_all_named_class_slices_when_budget_allows() -> None:
    file_body = "\n\n".join(
        [
            "class StringTrim:\n    def execute(self):\n        return 'trim'",
            "class StringReplace:\n    def execute(self):\n        return 'replace'",
            "class StringContains:\n    def execute(self):\n        return True",
            "class StringCompare:\n    def execute(self):\n        return False",
        ]
    )
    provider = MagicMock()
    provider.read_full_file.return_value = file_body
    task = ReviewTask(
        id="logic-string-manipulation",
        title="Diff-local correctness: String Manipulation",
        description=(
            "Audit StringTrim, StringReplace, StringContains, StringCompare. "
            "Do not review any other class in the target file."
        ),
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
    )
    state = {
        "run_id": "t",
        "git_diff": "",
        "metadata": {
            "mental_model": {
                "diff_surface_inventory": [
                    "StringTrim",
                    "StringReplace",
                    "StringContains",
                    "StringCompare",
                ]
            }
        },
    }
    ctx = ReviewTaskContext(file_snippets={"comfy_extras/nodes_string.py": file_body})

    bundle = build_task_evidence(state, task, provider, ctx)

    assert "class StringTrim" in bundle.rendered
    assert "class StringReplace" in bundle.rendered
    assert "class StringContains" in bundle.rendered
    assert "class StringCompare" in bundle.rendered
    assert not any("evidence_class_slice_omitted" in warning for warning in bundle.warnings)


def test_class_scoped_task_records_omitted_named_class_slices_when_budget_is_tight() -> None:
    large_body = "    def execute(self):\n" + ("        x = 1\n" * 100)
    file_body = "\n\n".join(
        [
            f"class Alpha:\n{large_body}",
            f"class Beta:\n{large_body}",
            f"class Gamma:\n{large_body}",
        ]
    )
    provider = MagicMock()
    provider.read_full_file.return_value = file_body
    task = ReviewTask(
        id="logic-large",
        title="Diff-local correctness",
        description="Audit Alpha, Beta, Gamma. Do not review any other class in the target file.",
        target_files=["pkg/large.py"],
        specialty="logic",
    )
    state = {
        "run_id": "t",
        "git_diff": "",
        "metadata": {"mental_model": {"diff_surface_inventory": ["Alpha", "Beta", "Gamma"]}},
    }
    settings = SimpleNamespace(
        reviewer_critique_packet_max_chars=3600,
        reviewer_critiquer_single_file_max_chars=20000,
        review_full_file_max_chars=20000,
    )

    bundle = build_task_evidence(state, task, provider, ReviewTaskContext(), settings=settings)

    assert "class Alpha" in bundle.rendered or "class Beta" in bundle.rendered
    assert any("evidence_class_slice_omitted:pkg/large.py:" in warning for warning in bundle.warnings)
    assert bundle.byte_chop is True


def test_task_evidence_reads_up_to_eighteen_target_files() -> None:
    target_files = [f"pkg/file_{idx}.py" for idx in range(20)]
    provider = MagicMock()
    provider.read_full_file.return_value = "def changed():\n    return True\n"
    task = ReviewTask(
        id="multi",
        title="Multi-file review",
        description="Review changed files",
        target_files=target_files,
    )

    bundle = build_task_evidence(_state(), task, provider, ReviewTaskContext())

    assert provider.read_full_file.call_count == 18
    assert "pkg/file_17.py" in bundle.file_contents
    assert "pkg/file_18.py" not in bundle.file_contents
    assert bundle.primary_files == ["pkg/file_0.py"]
    assert "pkg/file_0.py" in bundle.rendered
    assert "pkg/file_1.py" not in bundle.rendered
    assert "pkg/file_1.py" in bundle.omitted_prompt_files


def test_multi_file_task_prefers_explicit_named_surface_for_primary_prompt() -> None:
    provider = MagicMock()
    provider.read_full_file.side_effect = [
        "def helper():\n    return 'helper'\n",
        "def TargetSurface():\n    return 'target'\n",
    ]
    task = ReviewTask(
        id="multi-named",
        title="Diff-local correctness",
        description="Review TargetSurface behavior.",
        target_files=["pkg/helper.py", "pkg/target.py"],
    )
    state = _state(
        git_diff=(
            "diff --git a/pkg/helper.py b/pkg/helper.py\n+++ b/pkg/helper.py\n@@ -1 +1 @@\n+def helper():\n"
            "diff --git a/pkg/target.py b/pkg/target.py\n+++ b/pkg/target.py\n@@ -1 +1 @@\n+def TargetSurface():\n"
        )
    )
    ctx = ReviewTaskContext(
        entities_by_file={
            "pkg/helper.py": [
                CodeEntity(name="helper", type="function", signature="def helper", body="", definition_line=1),
            ],
            "pkg/target.py": [
                CodeEntity(
                    name="TargetSurface",
                    type="function",
                    signature="def TargetSurface",
                    body="def TargetSurface():\n    return 'target'",
                    definition_line=1,
                ),
            ],
        }
    )

    bundle = build_task_evidence(state, task, provider, ctx)

    assert bundle.primary_files == ["pkg/target.py"]
    assert "TargetSurface" in bundle.rendered
    assert "helper" not in bundle.rendered
    assert bundle.omitted_prompt_files == ["pkg/helper.py"]


def test_large_python_file_uses_ast_chunk_for_changed_top_level_function() -> None:
    prefix = "# header\n" * 80
    changed = "def target(value):\n    result = value + 1\n    return result\n"
    suffix = "\n".join(f"# filler {idx}" for idx in range(1200))
    body = prefix + changed + suffix
    provider = MagicMock()
    provider.read_full_file.return_value = body
    task = ReviewTask(
        id="large-python",
        title="Diff-local correctness",
        description="Review changed target behavior",
        target_files=["pkg/large.py"],
        specialty="logic",
    )
    state = _state(
        git_diff=(
            "diff --git a/pkg/large.py b/pkg/large.py\n"
            "+++ b/pkg/large.py\n"
            "@@ -81,3 +81,3 @@\n"
            "+def target(value):\n"
            "+    result = value + 1\n"
            "+    return result\n"
        )
    )
    settings = SimpleNamespace(
        reviewer_critique_packet_max_chars=5000,
        reviewer_critiquer_single_file_max_chars=50000,
        review_full_file_max_chars=50000,
    )

    bundle = build_task_evidence(state, task, provider, ReviewTaskContext(), settings=settings)

    assert "--- pkg/large.py: function target" in bundle.rendered
    assert "return result" in bundle.rendered
    assert "# filler 1000" not in bundle.rendered
    assert bundle.files_complete.get("pkg/large.py") is False


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


def test_critiquer_packet_keeps_diff_hunk_when_file_evidence_is_complete() -> None:
    body = "def execute():\n    return None\n"
    task = ReviewTask(
        id="t1",
        title="Check execute",
        description="Review execute behavior",
        target_files=["pkg/foo.py"],
        specialty="logic",
    )
    pipeline_slot = {
        "direct_context": "--- pkg/foo.py (complete file) ---\n" + body,
        "task_evidence": {
            "files_complete": {"pkg/foo.py": True},
            "file_contents": {"pkg/foo.py": body},
        },
    }

    packet = build_critiquer_packet(_state(), task, pipeline_slot)

    sections = {section.key: section.content for section in packet.sections}
    assert "diff_hunk" in sections
    assert "+++ b/pkg/foo.py" in sections["diff_hunk"]
    assert not packet.metadata.get("diff_hunk_suppressed")


def test_critiquer_packet_scopes_diff_to_primary_prompt_file() -> None:
    task = ReviewTask(
        id="multi",
        title="Multi-file review",
        description="Review changed files",
        target_files=["pkg/primary.py", "pkg/omitted.py"],
        specialty="logic",
    )
    state = _state(
        git_diff=(
            "diff --git a/pkg/primary.py b/pkg/primary.py\n+++ b/pkg/primary.py\n@@ -1 +1 @@\n+def primary():\n"
            "diff --git a/pkg/omitted.py b/pkg/omitted.py\n+++ b/pkg/omitted.py\n@@ -1 +1 @@\n+def omitted():\n"
        )
    )
    pipeline_slot = {
        "direct_context": "--- pkg/primary.py (file excerpt) ---\ndef primary():\n    return True\n",
        "task_evidence": {
            "primary_files": ["pkg/primary.py"],
            "omitted_prompt_files": ["pkg/omitted.py"],
            "files_complete": {"pkg/primary.py": False, "pkg/omitted.py": False},
        },
    }

    packet = build_critiquer_packet(state, task, pipeline_slot)

    sections = {section.key: section.content for section in packet.sections}
    assert "+++ b/pkg/primary.py" in sections["diff_hunk"]
    assert "+++ b/pkg/omitted.py" not in sections["diff_hunk"]
    assert "pkg/omitted.py" in sections["omitted_prompt_files"]
    assert packet.metadata["omitted_prompt_files"] == ["pkg/omitted.py"]
