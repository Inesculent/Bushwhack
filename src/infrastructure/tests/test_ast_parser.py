from typing import Any, Dict, List, Tuple

from src.infrastructure.cache.memory_cache import InMemoryCache
from src.infrastructure.mcp.ast_parser import MCPASTParser


class _FakeMCPClient:
    def __init__(self, responses: Dict[str, Dict[str, Any]]) -> None:
        self._responses = responses
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, arguments))
        return self._responses[name]


def test_get_file_structure_uses_cache_after_first_call() -> None:
    mcp_client = _FakeMCPClient(
        responses={
            "parse_file": {
                "entities": [
                    {
                        "name": "demo",
                        "type": "function",
                        "signature": "def demo():",
                        "body": "def demo():\n    return 1",
                        "dependencies": ["os"],
                    }
                ]
            }
        }
    )

    parser = MCPASTParser(
        mcp_client=mcp_client,
        cache=InMemoryCache(),
        parse_tool_name="parse_file",
        entity_tool_name="get_entity_details",
    )

    first = parser.get_file_structure("/repo", "src/example.py")
    second = parser.get_file_structure("/repo", "src/example.py")

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].name == "demo"
    assert second[0].dependencies == ["os"]
    assert len(mcp_client.calls) == 1


def test_get_entity_details_returns_none_when_not_found() -> None:
    mcp_client = _FakeMCPClient(
        responses={
            "get_entity_details": {
                "entity": None,
            }
        }
    )

    parser = MCPASTParser(
        mcp_client=mcp_client,
        cache=InMemoryCache(),
        parse_tool_name="parse_file",
        entity_tool_name="get_entity_details",
    )

    entity = parser.get_entity_details("/repo", "src/example.py", "missing")
    assert entity is None
    assert mcp_client.calls == [
        (
            "get_entity_details",
            {
                "repository_path": "/repo",
                "file_path": "src/example.py",
                "entity_name": "missing",
            },
        )
    ]


def test_get_file_structure_handles_result_envelope() -> None:
    mcp_client = _FakeMCPClient(
        responses={
            "parse_file": {
                "result": {
                    "entities": [
                        {
                            "name": "wrapped",
                            "type": "function",
                            "signature": "def wrapped():",
                            "body": "def wrapped():\n    return True",
                            "dependencies": [],
                        }
                    ]
                }
            }
        }
    )

    parser = MCPASTParser(
        mcp_client=mcp_client,
        cache=InMemoryCache(),
        parse_tool_name="parse_file",
        entity_tool_name="get_entity_details",
    )

    entities = parser.get_file_structure("/repo", "src/example.py")
    assert len(entities) == 1
    assert entities[0].name == "wrapped"
