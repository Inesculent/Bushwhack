from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
	"""Application settings loaded from environment variables."""

	model_config = SettingsConfigDict(
		env_file=".env",
		env_prefix="REVIEW_",
		extra="ignore",
	)

	ast_mcp_enabled: bool = Field(
		default=False,
		description="Enable the AST parser over MCP for repository understanding.",
	)
	ast_mcp_command: str = Field(
		default="python",
		description="Command used to start the AST MCP server process.",
	)
	ast_mcp_args: List[str] = Field(
		default_factory=lambda: ["mcp/fs-mcp/server.py"],
		description="Arguments for the AST MCP server command.",
	)
	ast_mcp_cwd: Optional[str] = Field(
		default=None,
		description="Optional working directory used when launching AST MCP server.",
	)
	ast_mcp_timeout_seconds: int = Field(
		default=30,
		ge=1,
		le=300,
		description="Timeout for each MCP request in seconds.",
	)
	ast_mcp_parse_tool: str = Field(
		default="parse_file",
		description="Tool name used to parse a file AST.",
	)
	ast_mcp_entity_tool: str = Field(
		default="get_entity_details",
		description="Tool name used to fetch a single entity from a file.",
	)
	ast_cache_ttl_seconds: int = Field(
		default=3600,
		ge=1,
		description="TTL for AST cache entries.",
	)
	ast_parser_version: str = Field(
		default="v1",
		description="Version tag included in AST cache keys for invalidation.",
	)
	ast_fallback_to_search: bool = Field(
		default=True,
		description="Keep non-AST fallback paths available when MCP is unavailable.",
	)
	redis_enabled: bool = Field(
		default=True,
		description="Enable Redis-backed LangGraph checkpointing.",
	)
	redis_url: str = Field(
		default="redis://localhost:6379/0",
		description="Redis connection URL used for LangGraph checkpointing.",
	)
	redis_namespace: str = Field(
		default="langgraph",
		description="Namespace prefix for Redis checkpoint keys.",
	)
	redis_ttl_seconds: int = Field(
		default=3600,
		ge=1,
		description="TTL for Redis checkpoint entries.",
	)
	github_personal_access_token: Optional[str] = Field(
		default=None,
		validation_alias=AliasChoices(
			"REVIEW_GITHUB_PERSONAL_ACCESS_TOKEN",
			"GITHUB_PERSONAL_ACCESS_TOKEN",
		),
		description="GitHub personal access token for PR API enrichment.",
	)

	def get_ast_mcp_cwd(self) -> str:
		"""Return an absolute working directory for MCP server startup."""
		if self.ast_mcp_cwd:
			return str(Path(self.ast_mcp_cwd).resolve())
		return str(Path(__file__).resolve().parents[1])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
	return Settings()
