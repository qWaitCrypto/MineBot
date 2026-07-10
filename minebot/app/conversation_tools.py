"""Shared tools for querying conversation turns outside the live model window."""

from __future__ import annotations

from typing import Protocol

from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult


class ConversationArchive(Protocol):
    def query_archive(
        self,
        *,
        query: str = "",
        start: int = 0,
        limit: int = 5,
    ) -> dict[str, object]: ...

    def read_archive_turn(
        self,
        handle: str,
        *,
        start: int = 0,
        limit: int = 20,
    ) -> dict[str, object] | None: ...


def register_conversation_archive_tools(
    registry: ToolRegistry,
    archive: ConversationArchive,
) -> None:
    registry.register(_query_tool(archive))
    registry.register(_read_tool(archive))


def _query_tool(archive: ConversationArchive) -> RegisteredTool:
    def query(params: dict[str, object]) -> ToolResult:
        result = archive.query_archive(
            query=str(params.get("query") or ""),
            start=int(params.get("start") or 0),
            limit=int(params.get("limit") or 5),
        )
        return ToolResult(True, "conversation_archive_query", False, metrics=result)

    return RegisteredTool(
        "query_conversation_archive",
        "Search complete closed conversation turns retained outside the live context window. Returns stable turn handles and bounded summaries.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 500},
                "start": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
        query,
        ToolSidecar(
            "query_conversation_archive",
            mutating=False,
            source="agent.context",
            tool_type="archive_query",
            permission="read_conversation_archive",
            body_scope=(),
            terminal_truth=("ConversationArchive.revision",),
        ),
    )


def _read_tool(archive: ConversationArchive) -> RegisteredTool:
    def read(params: dict[str, object]) -> ToolResult:
        handle = str(params.get("handle") or "")
        result = archive.read_archive_turn(
            handle,
            start=int(params.get("start") or 0),
            limit=int(params.get("limit") or 20),
        )
        if result is None:
            return ToolResult(
                False,
                "conversation_archive_handle_not_found",
                False,
                metrics={"handle": handle},
            )
        return ToolResult(True, "conversation_archive_read", False, metrics=result)

    return RegisteredTool(
        "read_conversation_archive",
        "Read one archived conversation turn by stable handle with complete tool call/result pairs and pagination.",
        {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "minLength": 1, "maxLength": 256},
                "start": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["handle"],
            "additionalProperties": False,
        },
        read,
        ToolSidecar(
            "read_conversation_archive",
            mutating=False,
            source="agent.context",
            tool_type="archive_query",
            permission="read_conversation_archive",
            body_scope=(),
            terminal_truth=("ConversationArchive.turn",),
        ),
    )


__all__ = ["ConversationArchive", "register_conversation_archive_tools"]
