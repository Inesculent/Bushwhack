from hashlib import sha256
from typing import Any, Dict, List, Optional, Sequence, cast

from src.domain.interfaces import IASTParser, ICacheService
from src.domain.schemas import CodeEntity, SymbolDefinition, SymbolDefinitionSource
from src.infrastructure.mcp.client import MCPClient


class MCPASTParser(IASTParser):
    """IASTParser implementation backed by a local MCP server."""

    def __init__(
        self,
        mcp_client: MCPClient,
        cache: ICacheService,
        parse_tool_name: str = "parse_file",
        entity_tool_name: str = "get_entity_details",
        definitions_tool_name: str = "find_symbol_definitions",
        cache_ttl_seconds: int = 3600,
        parser_version: str = "v1",
    ) -> None:
        self.mcp_client = mcp_client
        self.cache = cache
        self.parse_tool_name = parse_tool_name
        self.entity_tool_name = entity_tool_name
        self.definitions_tool_name = definitions_tool_name
        self.cache_ttl_seconds = cache_ttl_seconds
        self.parser_version = parser_version

    def get_file_structure(self, repository_path: str, file_path: str) -> List[CodeEntity]:
        cache_key = self._build_cache_key(repository_path, file_path, "structure")
        cached_payload = self.cache.get(cache_key)
        if cached_payload and isinstance(cached_payload.get("entities"), list):
            return [CodeEntity.model_validate(item) for item in cached_payload["entities"]]

        payload = self.mcp_client.call_tool(
            name=self.parse_tool_name,
            arguments={
                "repository_path": repository_path,
                "file_path": file_path,
            },
        )

        entities = self._map_entities_payload(payload)
        self.cache.set(
            key=cache_key,
            value={"entities": [entity.model_dump() for entity in entities]},
            expire=self.cache_ttl_seconds,
        )
        return entities

    def get_entity_details(
        self,
        repository_path: str,
        file_path: str,
        entity_name: str,
    ) -> Optional[CodeEntity]:
        cache_key = self._build_cache_key(repository_path, file_path, f"entity:{entity_name}")
        cached_payload = self.cache.get(cache_key)
        if cached_payload and isinstance(cached_payload.get("entity"), dict):
            return CodeEntity.model_validate(cached_payload["entity"])

        payload = self.mcp_client.call_tool(
            name=self.entity_tool_name,
            arguments={
                "repository_path": repository_path,
                "file_path": file_path,
                "entity_name": entity_name,
            },
        )

        raw_entity = payload.get("entity")
        if raw_entity is None and all(k in payload for k in ("name", "type", "signature", "body")):
            raw_entity = payload

        if raw_entity is None:
            return None

        entity = self._map_entity(raw_entity)
        self.cache.set(
            key=cache_key,
            value={"entity": entity.model_dump()},
            expire=self.cache_ttl_seconds,
        )
        return entity

    def find_symbol_definitions(
        self,
        repository_path: str,
        symbol_name: str,
        *,
        candidate_file_paths: Sequence[str] | None = None,
        max_results: int = 50,
    ) -> List[SymbolDefinition]:
        payload = self.mcp_client.call_tool(
            name=self.definitions_tool_name,
            arguments={
                "repository_path": repository_path,
                "symbol_name": symbol_name,
                "candidate_file_paths": list(candidate_file_paths) if candidate_file_paths else None,
                "max_results": max_results,
            },
        )
        return self._map_definitions_payload(payload)

    def _build_cache_key(self, repository_path: str, file_path: str, purpose: str) -> str:
        seed = f"{self.parser_version}|{repository_path}|{file_path}|{purpose}"
        digest = sha256(seed.encode("utf-8")).hexdigest()
        return f"ast:{digest}"

    def _map_entities_payload(self, payload: Dict[str, Any]) -> List[CodeEntity]:
        if isinstance(payload.get("result"), dict):
            payload = payload["result"]

        raw_entities = payload.get("entities", [])
        if not isinstance(raw_entities, list):
            return []
        return [self._map_entity(raw_entity) for raw_entity in raw_entities if isinstance(raw_entity, dict)]

    @staticmethod
    def _map_entity(raw_entity: Dict[str, Any]) -> CodeEntity:
        dependencies = raw_entity.get("dependencies")
        if not isinstance(dependencies, list):
            dependencies = []

        definition_line = raw_entity.get("definition_line")
        dl: Optional[int] = None
        if definition_line is not None:
            try:
                dl = int(definition_line)
            except (TypeError, ValueError):
                dl = None
        definition_end_line = raw_entity.get("definition_end_line")
        del_: Optional[int] = None
        if definition_end_line is not None:
            try:
                del_ = int(definition_end_line)
            except (TypeError, ValueError):
                del_ = None

        return CodeEntity(
            name=str(raw_entity.get("name", "unknown")),
            type=str(raw_entity.get("type", "unknown")),
            signature=str(raw_entity.get("signature", "")),
            body=str(raw_entity.get("body", "")),
            dependencies=[str(dep) for dep in dependencies],
            definition_line=dl,
            definition_end_line=del_,
        )

    def _map_definitions_payload(self, payload: Dict[str, Any]) -> List[SymbolDefinition]:
        if isinstance(payload.get("result"), dict):
            payload = payload["result"]
        raw_defs = payload.get("definitions", [])
        if not isinstance(raw_defs, list):
            return []
        out: List[SymbolDefinition] = []
        for item in raw_defs:
            if not isinstance(item, dict):
                continue
            try:
                raw_src = item.get("source", "mcp")
                if raw_src not in ("jedi", "tree_sitter", "regex", "mcp"):
                    raw_src = "mcp"
                out.append(
                    SymbolDefinition(
                        file_path=str(item.get("file_path", "")).replace("\\", "/"),
                        line_start=int(item.get("line_start", 1)),
                        entity_name=str(item.get("entity_name", "")),
                        entity_type=str(item.get("entity_type", "unknown")),
                        signature=str(item.get("signature", "")),
                        source=cast(SymbolDefinitionSource, raw_src),
                    )
                )
            except Exception:
                continue
        return out
