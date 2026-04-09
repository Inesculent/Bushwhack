from types import SimpleNamespace

import pytest

from src.infrastructure.mcp.client import MCPClient, MCPClientError, MCPToolError


class _DummyResult:
    def __init__(self, *, content=None, structured=None, is_error=False) -> None:
        self.content = content or []
        self.structuredContent = structured
        self.isError = is_error


def test_normalize_tool_result_prefers_structured_content() -> None:
    result = _DummyResult(structured={"entities": []}, content=[SimpleNamespace(type="text", text="ignored")])
    normalized = MCPClient._normalize_tool_result("parse_file", result)
    assert normalized == {"entities": []}


def test_normalize_tool_result_unwraps_fastmcp_result_envelope() -> None:
    result = _DummyResult(structured={"result": {"entities": []}})
    normalized = MCPClient._normalize_tool_result("parse_file", result)
    assert normalized == {"entities": []}


def test_normalize_tool_result_parses_json_text_payload() -> None:
    result = _DummyResult(content=[SimpleNamespace(type="text", text='{"entity": {"name": "x"}}')])
    normalized = MCPClient._normalize_tool_result("get_entity_details", result)
    assert normalized == {"entity": {"name": "x"}}


def test_normalize_tool_result_raises_on_tool_error() -> None:
    result = _DummyResult(is_error=True, content=[SimpleNamespace(type="text", text="boom")])
    with pytest.raises(MCPToolError, match="boom"):
        MCPClient._normalize_tool_result("parse_file", result)


def test_call_tool_wraps_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MCPClient(command="python", args=["-V"])  # Command is irrelevant in this mocked path.

    async def _raise_timeout(name, arguments=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(client, "call_tool_async", _raise_timeout)

    with pytest.raises(MCPClientError, match="timed out"):
        client.call_tool("parse_file", {"file_path": "x.py"})
