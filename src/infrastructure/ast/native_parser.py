import re
from hashlib import sha256
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from src.domain.interfaces import IASTParser, ICacheService
from src.domain.schemas import CodeEntity, SymbolDefinition


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

    _SKIP_PATH_SEGMENTS = {
        ".git",
        ".venv",
        "node_modules",
        "vendor",
        "third_party",
        "external",
        "deps",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }

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

    def find_symbol_definitions(
        self,
        repository_path: str,
        symbol_name: str,
        *,
        candidate_file_paths: Sequence[str] | None = None,
        max_results: int = 50,
    ) -> List[SymbolDefinition]:
        repo_root = Path(repository_path).resolve()
        if not repo_root.is_dir():
            return []

        results: List[SymbolDefinition] = []
        seen: Set[Tuple[str, int, str]] = set()

        for definition in self._jedi_symbol_definitions(str(repo_root), symbol_name, max_results=max_results):
            key = (definition.file_path, definition.line_start, definition.entity_name)
            if key in seen:
                continue
            seen.add(key)
            results.append(definition)
            if len(results) >= max_results:
                return results

        for rel_path in self._iter_candidate_relative_paths(repo_root, candidate_file_paths):
            if len(results) >= max_results:
                break
            try:
                language = self._detect_language(rel_path)
            except ValueError:
                continue
            try:
                source = self._safe_file_read(str(repo_root), rel_path)
            except (OSError, FileNotFoundError, ValueError):
                continue
            for entity in self._collect_entities(source=source, language=language):
                if not self._symbol_matches(entity.name, symbol_name):
                    continue
                line_start = entity.definition_line or 1
                key = (rel_path.replace("\\", "/"), line_start, entity.name)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    SymbolDefinition(
                        file_path=rel_path.replace("\\", "/"),
                        line_start=line_start,
                        entity_name=entity.name,
                        entity_type=entity.type,
                        signature=entity.signature,
                        source="tree_sitter",
                    )
                )
                if len(results) >= max_results:
                    return results

        return results

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

            definition_line = int(node.start_point[0]) + 1
            definition_end_line = int(node.end_point[0]) + 1
            entities.append(
                CodeEntity(
                    name=cls._node_name(node, source_bytes),
                    type=cls._normalize_entity_type(node.type),
                    signature=signature,
                    body=body,
                    dependencies=cls._extract_dependencies(body),
                    definition_line=definition_line,
                    definition_end_line=definition_end_line,
                )
            )

        return entities

    @staticmethod
    def _symbol_matches(entity_name: str, symbol_name: str) -> bool:
        if entity_name == symbol_name:
            return True
        if entity_name.endswith(f".{symbol_name}"):
            return True
        if "." in symbol_name:
            return NativeASTParser._symbol_matches(entity_name, symbol_name.split(".")[-1])
        return False

    def _iter_candidate_relative_paths(
        self,
        repo_root: Path,
        candidate_file_paths: Sequence[str] | None,
        *,
        max_files: int = 8000,
    ) -> List[str]:
        extensions = set(self._LANGUAGE_BY_EXTENSION.keys())
        collected: List[str] = []

        if candidate_file_paths:
            for raw in candidate_file_paths:
                normalized = raw.replace("\\", "/").lstrip("/")
                if not normalized or ".." in normalized.split("/"):
                    continue
                target = (repo_root / normalized).resolve()
                try:
                    target.relative_to(repo_root)
                except ValueError:
                    continue
                if target.is_file() and target.suffix.lower() in extensions:
                    collected.append(normalized.replace("\\", "/"))
            return collected

        count = 0
        for path in repo_root.rglob("*"):
            if count >= max_files:
                break
            if not path.is_file():
                continue
            try:
                path.relative_to(repo_root)
            except ValueError:
                continue
            if path.suffix.lower() not in extensions:
                continue
            if any(seg in self._SKIP_PATH_SEGMENTS for seg in path.parts):
                continue
            collected.append(path.relative_to(repo_root).as_posix())
            count += 1
        collected.sort()
        return collected

    @staticmethod
    def _jedi_symbol_definitions(repo_root: str, symbol_name: str, *, max_results: int) -> List[SymbolDefinition]:
        try:
            from jedi import Project  # type: ignore[import-untyped]
        except ImportError:
            return []

        root_path = Path(repo_root).resolve()
        try:
            project = Project(str(root_path))
        except Exception:
            return []

        out: List[SymbolDefinition] = []
        seen: Set[Tuple[str, int, str]] = set()
        try:
            for completion in project.complete_search(symbol_name, all_scopes=True):
                if len(out) >= max_results:
                    break
                try:
                    for definition in completion.goto():
                        module_path = definition.module_path
                        if module_path is None:
                            continue
                        abs_path = Path(str(module_path)).resolve()
                        try:
                            rel = abs_path.relative_to(root_path).as_posix()
                        except ValueError:
                            continue
                        line_start = int(definition.line or 1)
                        name = str(definition.name or symbol_name)
                        typ = str(definition.type or "unknown")
                        try:
                            signature = definition.get_line_code().strip()
                        except Exception:
                            signature = ""
                        key = (rel, line_start, name)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(
                            SymbolDefinition(
                                file_path=rel,
                                line_start=line_start,
                                entity_name=name,
                                entity_type=typ,
                                signature=signature,
                                source="jedi",
                            )
                        )
                        if len(out) >= max_results:
                            return out
                except Exception:
                    continue
        except Exception:
            return []

        return out
