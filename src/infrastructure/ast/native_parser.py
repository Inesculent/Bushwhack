import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from src.domain.interfaces import IASTParser, ICacheService
from src.domain.schemas import CodeEntity


class NativeASTParser(IASTParser):
    """IASTParser implementation using in-process tree-sitter bindings."""

    _LANGUAGE_BY_EXTENSION = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".cs": "c_sharp",
        ".php": "php",
        ".rb": "ruby",
    }

    _ENTITY_NODE_TYPES = {
        "function_definition",
        "method_definition",
        "class_definition",
        "function_declaration",
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "struct_item",
        "impl_item",
    }

    _IMPORT_PATTERN = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_\.]+)", re.MULTILINE)

    def __init__(
        self,
        cache: ICacheService,
        cache_ttl_seconds: int = 3600,
        parser_version: str = "v1-native",
    ) -> None:
        self.cache = cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self.parser_version = parser_version

    def get_file_structure(self, repository_path: str, file_path: str) -> List[CodeEntity]:
        cache_key = self._build_cache_key(repository_path, file_path, "structure")
        cached_payload = self.cache.get(cache_key)
        if cached_payload and isinstance(cached_payload.get("entities"), list):
            return [CodeEntity.model_validate(item) for item in cached_payload["entities"]]

        source = self._safe_file_read(repository_path=repository_path, file_path=file_path)
        language = self._detect_language(file_path=file_path)
        entities = self._collect_entities(source=source, language=language)

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

        entities = self.get_file_structure(repository_path=repository_path, file_path=file_path)
        matched: Optional[CodeEntity] = None
        for entity in entities:
            if entity.name == entity_name or entity.name.endswith(f".{entity_name}"):
                matched = entity
                break

        if matched is None:
            return None

        self.cache.set(
            key=cache_key,
            value={"entity": matched.model_dump()},
            expire=self.cache_ttl_seconds,
        )
        return matched

    def _build_cache_key(self, repository_path: str, file_path: str, purpose: str) -> str:
        seed = f"{self.parser_version}|{repository_path}|{file_path}|{purpose}"
        digest = sha256(seed.encode("utf-8")).hexdigest()
        return f"ast:{digest}"

    @classmethod
    def _safe_file_read(cls, repository_path: str, file_path: str) -> str:
        repo_root = Path(repository_path).resolve()
        target_path = (repo_root / file_path).resolve()

        try:
            target_path.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError("file_path must be inside repository_path") from exc

        if not target_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        return target_path.read_text(encoding="utf-8", errors="replace")

    @classmethod
    def _detect_language(cls, file_path: str) -> str:
        suffix = Path(file_path).suffix.lower()
        language = cls._LANGUAGE_BY_EXTENSION.get(suffix)
        if language is None:
            raise ValueError(f"Unsupported file extension for AST parsing: {suffix or '<none>'}")
        return language

    @staticmethod
    def _node_name(node: Node, source_bytes: bytes) -> str:
        for field_name in ("name", "declarator"):
            named_node = node.child_by_field_name(field_name)
            if named_node is not None:
                return source_bytes[named_node.start_byte : named_node.end_byte].decode("utf-8", errors="replace")

        for child in node.children:
            if child.type in {"identifier", "type_identifier", "property_identifier"}:
                return source_bytes[child.start_byte : child.end_byte].decode("utf-8", errors="replace")

        return f"{node.type}@{node.start_point[0] + 1}"

    @classmethod
    def _node_is_entity(cls, node: Node) -> bool:
        if node.type in cls._ENTITY_NODE_TYPES:
            return True

        if node.child_by_field_name("name") is None:
            return False

        lower_type = node.type.lower()
        return any(token in lower_type for token in ("function", "method", "class", "interface", "enum", "struct"))

    @staticmethod
    def _normalize_entity_type(node_type: str) -> str:
        lowered = node_type.lower()
        if "class" in lowered:
            return "class"
        if "method" in lowered or "function" in lowered:
            return "function"
        if "interface" in lowered:
            return "interface"
        if "enum" in lowered:
            return "enum"
        if "struct" in lowered:
            return "struct"
        return "entity"

    @classmethod
    def _extract_dependencies(cls, source: str) -> List[str]:
        deps = {match.group(1) for match in cls._IMPORT_PATTERN.finditer(source)}
        return sorted(deps)

    @classmethod
    def _collect_entities(cls, source: str, language: str) -> List[CodeEntity]:
        parser = get_parser(language)
        source_bytes = source.encode("utf-8")
        tree = parser.parse(source_bytes)
        lines = source.splitlines()

        entities: List[CodeEntity] = []
        stack: List[Node] = [tree.root_node]

        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))

            if not cls._node_is_entity(node):
                continue

            start_line = node.start_point[0]
            signature = lines[start_line].strip() if 0 <= start_line < len(lines) else ""
            body = source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

            entities.append(
                CodeEntity(
                    name=cls._node_name(node, source_bytes),
                    type=cls._normalize_entity_type(node.type),
                    signature=signature,
                    body=body,
                    dependencies=cls._extract_dependencies(body),
                )
            )

        return entities