import asyncio
import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPClientError(RuntimeError):
    """Raised when a low-level MCP client operation fails."""


class MCPToolError(MCPClientError):
    """Raised when an MCP tool returns an error response."""


class MCPClient:
    """Thin synchronous wrapper over MCP stdio transport."""

    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call a tool in a short-lived MCP session."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No active loop in the current thread, this is the expected sync path.
            pass
        else:
            raise MCPClientError(
                "MCPClient.call_tool() cannot run inside an active event loop. "
                "Use call_tool_async() in async contexts."
            )

        try:
            return asyncio.run(self.call_tool_async(name=name, arguments=arguments))
        except TimeoutError as exc:
            raise MCPClientError(f"MCP tool call timed out for '{name}'.") from exc
        except MCPClientError:
            raise
        except Exception as exc:
            raise MCPClientError(
                f"MCP tool call failed for '{name}': {self._describe_exception(exc)}"
            ) from exc

    def list_tools(self) -> List[str]:
        """Return tool names advertised by the MCP server."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise MCPClientError(
                "MCPClient.list_tools() cannot run inside an active event loop. "
                "Use list_tools_async() in async contexts."
            )

        try:
            return asyncio.run(self.list_tools_async())
        except TimeoutError as exc:
            raise MCPClientError("MCP list_tools call timed out.") from exc
        except MCPClientError:
            raise
        except Exception as exc:
            raise MCPClientError(f"MCP list_tools call failed: {self._describe_exception(exc)}") from exc

    async def list_tools_async(self) -> List[str]:
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
        )
        timeout = timedelta(seconds=self.timeout_seconds)

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timeout,
            ) as session:
                await session.initialize()
                result = await session.list_tools()

        tools = getattr(result, "tools", []) or []
        return [
            str(getattr(tool, "name", ""))
            for tool in tools
            if str(getattr(tool, "name", "")).strip()
        ]

    async def call_tool_async(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
        )
        timeout = timedelta(seconds=self.timeout_seconds)

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timeout,
            ) as session:
                await session.initialize()
                result = await session.call_tool(
                    name=name,
                    arguments=arguments or {},
                    read_timeout_seconds=timeout,
                )

        return self._normalize_tool_result(tool_name=name, result=result)

    @staticmethod
    def _normalize_tool_result(tool_name: str, result: Any) -> Dict[str, Any]:
        if bool(getattr(result, "isError", False)):
            error_text = MCPClient._extract_text_content(result)
            if not error_text:
                error_text = "No error details returned by MCP server"
            raise MCPToolError(f"MCP tool '{tool_name}' returned an error: {error_text}")

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            # FastMCP tool responses often arrive as {"result": <tool-return>}.
            if set(structured.keys()) == {"result"} and isinstance(structured["result"], dict):
                return structured["result"]
            return structured
        if structured is not None:
            return {"result": structured}

        text_content = MCPClient._extract_text_content(result).strip()
        if not text_content:
            return {}

        try:
            payload = json.loads(text_content)
        except json.JSONDecodeError:
            return {"text": text_content}

        if isinstance(payload, dict):
            return payload
        return {"result": payload}

    @staticmethod
    def _extract_text_content(result: Any) -> str:
        chunks: List[str] = []
        content_items = getattr(result, "content", []) or []

        for item in content_items:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                continue

            item_type = getattr(item, "type", None)
            if item_type == "text":
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    chunks.append(text)

        return "\n".join(chunks)

    @staticmethod
    def _describe_exception(exc: Exception) -> str:
        def _flatten(current: BaseException, seen: set[int]) -> List[str]:
            if id(current) in seen:
                return []
            seen.add(id(current))
            rows = [f"{current.__class__.__name__}: {current}"]
            for child in list(getattr(current, "exceptions", []) or [])[:3]:
                rows.extend(_flatten(child, seen))
            for attr in ("__cause__", "__context__"):
                inner = getattr(current, attr, None)
                if inner is not None:
                    rows.extend(_flatten(inner, seen))
            return rows

        nested = getattr(exc, "exceptions", None)
        if not nested:
            return str(exc)
        details = []
        seen: set[int] = {id(exc)}
        for item in list(nested)[:3]:
            details.extend(_flatten(item, seen))
        suffix = "" if len(nested) <= 3 else f"; +{len(nested) - 3} more"
        return f"{exc} ({'; '.join(dict.fromkeys(details))}{suffix})"
