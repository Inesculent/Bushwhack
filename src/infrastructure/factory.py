import logging
from dataclasses import dataclass
from typing import Optional

from src.config import Settings, get_settings
from src.domain.interfaces import IASTParser, ICacheService, ICodeSearcher
from src.infrastructure.cache.memory_cache import InMemoryCache
from src.infrastructure.mcp.ast_parser import MCPASTParser
from src.infrastructure.mcp.client import MCPClient
from src.infrastructure.sandbox import RepoSandbox
from src.infrastructure.search.ripgrep import RipgrepSearcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepositoryUnderstandingAdapters:
    searcher: ICodeSearcher
    ast_parser: Optional[IASTParser]
    ast_enabled: bool


def build_cache_service() -> ICacheService:
    return InMemoryCache()


def build_ast_parser(settings: Settings, cache: ICacheService) -> IASTParser:
    mcp_client = MCPClient(
        command=settings.ast_mcp_command,
        args=settings.ast_mcp_args,
        cwd=settings.get_ast_mcp_cwd(),
        timeout_seconds=settings.ast_mcp_timeout_seconds,
    )
    return MCPASTParser(
        mcp_client=mcp_client,
        cache=cache,
        parse_tool_name=settings.ast_mcp_parse_tool,
        entity_tool_name=settings.ast_mcp_entity_tool,
        cache_ttl_seconds=settings.ast_cache_ttl_seconds,
        parser_version=settings.ast_parser_version,
    )


def build_repository_understanding_adapters(
    sandbox: RepoSandbox,
    settings: Optional[Settings] = None,
    cache: Optional[ICacheService] = None,
) -> RepositoryUnderstandingAdapters:
    resolved_settings = settings or get_settings()
    resolved_cache = cache or build_cache_service()

    searcher = RipgrepSearcher(sandbox=sandbox)
    ast_parser: Optional[IASTParser] = None

    if resolved_settings.ast_mcp_enabled:
        try:
            ast_parser = build_ast_parser(settings=resolved_settings, cache=resolved_cache)
        except Exception as exc:
            if not resolved_settings.ast_fallback_to_search:
                raise
            logger.warning(
                "AST parser startup failed; continuing with search fallback. reason=%s",
                exc,
            )

    return RepositoryUnderstandingAdapters(
        searcher=searcher,
        ast_parser=ast_parser,
        ast_enabled=ast_parser is not None,
    )
