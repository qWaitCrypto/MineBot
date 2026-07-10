"""Persistent, queryable full-fidelity artifacts for bounded tool observations."""

from __future__ import annotations

from typing import Protocol

from minebot.app.observability import sanitize_observation
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import JsonObject, ToolResult


class ToolObservationArchive(Protocol):
    def store(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        result: JsonObject,
        complete: bool | None,
    ) -> str: ...

    def query(
        self,
        *,
        query: str = "",
        tool_name: str = "",
        reason: str = "",
        start: int = 0,
        limit: int = 10,
    ) -> dict[str, object]: ...

    def read(
        self,
        handle: str,
        *,
        path: list[str | int] | None = None,
        start: int = 0,
        limit: int = 20,
        max_chars: int = 2000,
    ) -> dict[str, object] | None: ...


class ObservationPathError(ValueError):
    """A requested observation field path does not exist."""


class PersistentToolObservationArchive:
    """Scope-isolated adapter over the control-plane SQLite artifact store."""

    def __init__(self, store: RuntimeStateStore, scope: RuntimeScope) -> None:
        self._state_store = store
        self.scope = scope

    def store(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        result: JsonObject,
        complete: bool | None,
    ) -> str:
        safe_result = sanitize_observation(result)
        if not isinstance(safe_result, dict):
            raise ValueError("tool observation result must be an object")
        record = self._state_store.create_tool_observation(
            self.scope,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=safe_result,
            complete=complete,
        )
        return str(record["handle"])

    def query(
        self,
        *,
        query: str = "",
        tool_name: str = "",
        reason: str = "",
        start: int = 0,
        limit: int = 10,
    ) -> dict[str, object]:
        return self._state_store.query_tool_observations(
            self.scope,
            query=query,
            tool_name=tool_name,
            reason=reason,
            start=start,
            limit=limit,
        )

    def read(
        self,
        handle: str,
        *,
        path: list[str | int] | None = None,
        start: int = 0,
        limit: int = 20,
        max_chars: int = 2000,
    ) -> dict[str, object] | None:
        record = self._state_store.get_tool_observation(self.scope, handle)
        if record is None:
            return None
        normalized_path = _normalize_path(path)
        selected = _select_path(record["result"], normalized_path)
        return {
            "handle": record["handle"],
            "tool": record["tool"],
            "tool_call_id": record["tool_call_id"],
            "success": record["success"],
            "reason": record["reason"],
            "source_complete": record["complete"],
            "payload_bytes": record["payload_bytes"],
            "created_at": record["created_at"],
            "path": normalized_path,
            **_page_value(
                selected,
                start=start,
                limit=limit,
                max_chars=max_chars,
            ),
        }


def register_tool_observation_tools(
    registry: ToolRegistry,
    archive: ToolObservationArchive,
) -> None:
    registry.register(_query_tool(archive))
    registry.register(_read_tool(archive))


def _query_tool(archive: ToolObservationArchive) -> RegisteredTool:
    def query(params: dict[str, object]) -> ToolResult:
        result = archive.query(
            query=str(params.get("query") or ""),
            tool_name=str(params.get("tool") or ""),
            reason=str(params.get("reason") or ""),
            start=int(params.get("start") or 0),
            limit=int(params.get("limit") or 10),
        )
        return ToolResult(True, "tool_observation_query", False, metrics=result)

    return RegisteredTool(
        "query_tool_observations",
        "Search persisted full tool-result artifacts by tool, reason, or content. Returns stable observation handles and bounded metadata.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 500},
                "tool": {"type": "string", "maxLength": 128},
                "reason": {"type": "string", "maxLength": 512},
                "start": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
        query,
        ToolSidecar(
            "query_tool_observations",
            mutating=False,
            source="agent.context",
            tool_type="artifact_query",
            permission="read_tool_observations",
            body_scope=(),
            terminal_truth=("ToolObservationArchive.revision",),
        ),
    )


def _read_tool(archive: ToolObservationArchive) -> RegisteredTool:
    def read(params: dict[str, object]) -> ToolResult:
        handle = str(params.get("handle") or "")
        raw_path = params.get("path")
        try:
            result = archive.read(
                handle,
                path=None if raw_path is None else list(raw_path),
                start=int(params.get("start") or 0),
                limit=int(params.get("limit") or 20),
                max_chars=int(params.get("max_chars") or 2000),
            )
        except (ObservationPathError, TypeError, ValueError) as exc:
            return ToolResult(
                False,
                "tool_observation_path_not_found",
                False,
                metrics={"handle": handle, "path": raw_path, "error": str(exc)},
            )
        if result is None:
            return ToolResult(
                False,
                "tool_observation_handle_not_found",
                False,
                metrics={"handle": handle},
            )
        return ToolResult(True, "tool_observation_read", False, metrics=result)

    return RegisteredTool(
        "read_tool_observation",
        "Read a persisted full tool result by observation handle. Select a nested field with a structured path and page lists, objects, or long strings.",
        {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "minLength": 1, "maxLength": 256},
                "path": {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "integer"},
                        ]
                    },
                    "maxItems": 16,
                },
                "start": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_chars": {"type": "integer", "minimum": 100, "maximum": 4000},
            },
            "required": ["handle"],
            "additionalProperties": False,
        },
        read,
        ToolSidecar(
            "read_tool_observation",
            mutating=False,
            source="agent.context",
            tool_type="artifact_query",
            permission="read_tool_observations",
            body_scope=(),
            terminal_truth=("ToolObservationArchive.payload",),
        ),
    )


def _normalize_path(path: list[str | int] | None) -> list[str | int]:
    if path is None:
        return []
    if not isinstance(path, list) or len(path) > 16:
        raise ObservationPathError("path must be a list with at most 16 segments")
    normalized: list[str | int] = []
    for segment in path:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)):
            raise ObservationPathError("path segments must be strings or integers")
        if isinstance(segment, str):
            if not segment or len(segment) > 256:
                raise ObservationPathError("string path segments must contain 1-256 characters")
            normalized.append(segment)
        else:
            normalized.append(int(segment))
    return normalized


def _select_path(value: object, path: list[str | int]) -> object:
    selected = value
    for segment in path:
        if isinstance(selected, dict) and isinstance(segment, str) and segment in selected:
            selected = selected[segment]
            continue
        if isinstance(selected, list) and isinstance(segment, int) and -len(selected) <= segment < len(selected):
            selected = selected[segment]
            continue
        raise ObservationPathError(f"observation path does not exist at segment {segment!r}")
    return selected


def _page_value(
    value: object,
    *,
    start: int,
    limit: int,
    max_chars: int,
) -> dict[str, object]:
    start = max(0, int(start))
    limit = max(1, min(100, int(limit)))
    max_chars = max(100, min(4000, int(max_chars)))
    if isinstance(value, list):
        page = value[start : start + limit]
        next_start = start + len(page) if start + len(page) < len(value) else None
        return {
            "value_type": "list",
            "start": start,
            "limit": limit,
            "total_count": len(value),
            "items": page,
            "next_start": next_start,
            "omitted_count": len(value) - len(page),
            "complete": next_start is None,
        }
    if isinstance(value, dict):
        entries = sorted(value.items(), key=lambda item: str(item[0]))
        page = entries[start : start + limit]
        next_start = start + len(page) if start + len(page) < len(entries) else None
        return {
            "value_type": "object",
            "start": start,
            "limit": limit,
            "total_count": len(entries),
            "value": {str(key): item for key, item in page},
            "next_start": next_start,
            "omitted_count": len(entries) - len(page),
            "complete": next_start is None,
        }
    if isinstance(value, str):
        page = value[start : start + max_chars]
        next_start = start + len(page) if start + len(page) < len(value) else None
        return {
            "value_type": "string",
            "start": start,
            "max_chars": max_chars,
            "char_count": len(value),
            "value": page,
            "next_start": next_start,
            "omitted_count": len(value) - len(page),
            "complete": next_start is None,
        }
    return {
        "value_type": type(value).__name__,
        "value": value,
        "complete": True,
        "omitted_count": 0,
        "next_start": None,
    }


__all__ = [
    "ObservationPathError",
    "PersistentToolObservationArchive",
    "ToolObservationArchive",
    "register_tool_observation_tools",
]
