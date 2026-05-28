"""Tests for in-sandbox AST entity extraction."""

from __future__ import annotations

import json

from src.infrastructure.sandbox_ast import collect_sandbox_file_entities, entities_from_sandbox_payload


class _FakeSandbox:
    def __init__(self, output: str) -> None:
        self._output = output
        self.last_command: list[str] | None = None

    def execute(self, command: list[str], workdir: str = "/repo", check_exit_code: bool = False) -> str:
        self.last_command = command
        return self._output


def test_entities_from_sandbox_payload_deserializes() -> None:
    payload = {
        "files": {
            "pkg/mod.py": [
                {
                    "name": "Foo",
                    "type": "class",
                    "signature": "class Foo:",
                    "body": "class Foo:\n    pass",
                    "dependencies": [],
                    "definition_line": 1,
                }
            ]
        },
        "gaps": [],
    }
    result = entities_from_sandbox_payload(payload)
    assert "pkg/mod.py" in result
    assert result["pkg/mod.py"][0].name == "Foo"


def test_collect_sandbox_file_entities_invokes_script() -> None:
    payload = {"files": {}, "gaps": []}
    sandbox = _FakeSandbox(json.dumps(payload))
    out = collect_sandbox_file_entities(sandbox, ["a.py"])
    assert out == payload
    assert sandbox.last_command is not None
    assert sandbox.last_command[0] == "python"


def test_collect_sandbox_file_entities_recovers_json_from_noisy_output() -> None:
    payload = {
        "files": {
            "a.py": [
                {
                    "name": "A",
                    "type": "class",
                    "signature": "class A:",
                    "body": "class A:\n    pass",
                    "dependencies": [],
                    "definition_line": 1,
                }
            ]
        },
        "gaps": [],
    }
    sandbox = _FakeSandbox(f"warning: optional parser unavailable\n{json.dumps(payload)}\ntrailing noise")
    out = collect_sandbox_file_entities(sandbox, ["a.py"])
    assert out == payload
