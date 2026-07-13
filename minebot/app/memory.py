"""Scoped agentic long-term memory and its shared tool surface."""

from __future__ import annotations

from dataclasses import dataclass

from minebot.app.runtime_state import (
    MemoryKind,
    MemoryRecord,
    MemorySource,
    MemoryStateConflict,
    RuntimeScope,
    RuntimeStateStore,
    memory_record_payload,
)
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult


@dataclass
class MemoryWorkspace:
    store: RuntimeStateStore
    scope: RuntimeScope

    def write(
        self,
        *,
        kind: MemoryKind,
        source: MemorySource,
        title: str,
        content: str,
        subject_key: str = "",
        evidence_ref: str = "",
        dimension: str | None = None,
        point: tuple[float, float, float] | None = None,
        region: tuple[float, float, float, float, float, float] | None = None,
    ) -> MemoryRecord:
        return self.store.create_memory(
            self.scope,
            kind=kind,
            source=source,
            title=title,
            content=content,
            subject_key=subject_key,
            evidence_ref=evidence_ref,
            dimension=dimension,
            point=point,
            region=region,
        )

    def read(self, memory_id: str) -> MemoryRecord | None:
        return self.store.get_memory(self.scope, memory_id)

    def update(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        changes: dict[str, object],
    ) -> MemoryRecord:
        current = self.read(memory_id)
        if current is None:
            raise MemoryStateConflict(f"memory not found in scope: {memory_id}")
        clear_geometry = bool(changes.get("clear_geometry", False))
        point = None if clear_geometry else current.point
        region = None if clear_geometry else current.region
        if "point" in changes:
            point = _point(changes.get("point"))
            region = None
        if "region" in changes:
            region = _region(changes.get("region"))
            point = None
        return self.store.update_memory(
            self.scope,
            memory_id,
            expected_revision=expected_revision,
            kind=MemoryKind(str(changes.get("kind") or current.kind.value)),
            source=MemorySource(str(changes.get("source") or current.source.value)),
            title=str(changes.get("title") or current.title),
            content=str(changes.get("content") or current.content),
            subject_key=(
                current.subject_key
                if "subject_key" not in changes
                else str(changes.get("subject_key") or "")
            ),
            evidence_ref=(
                current.evidence_ref
                if "evidence_ref" not in changes
                else str(changes.get("evidence_ref") or "")
            ),
            dimension=(
                current.dimension
                if "dimension" not in changes
                else str(changes.get("dimension") or "") or None
            ),
            point=point,
            region=region,
        )

    def delete(self, memory_id: str, *, expected_revision: int) -> None:
        self.store.delete_memory(
            self.scope,
            memory_id,
            expected_revision=expected_revision,
        )

    def search(self, params: dict[str, object]) -> dict[str, object]:
        return self.store.search_memories(
            self.scope,
            query=str(params.get("query") or ""),
            kinds=tuple(MemoryKind(str(item)) for item in (params.get("kinds") or [])),
            sources=tuple(
                MemorySource(str(item)) for item in (params.get("sources") or [])
            ),
            subject_key=str(params.get("subject_key") or ""),
            dimension=str(params.get("dimension") or "") or None,
            center=_point(params.get("center")),
            radius=(None if params.get("radius") is None else float(params["radius"])),
            region=_region(params.get("region")),
            start=int(params.get("start") or 0),
            limit=int(params.get("limit") or 10),
        )


def register_memory_tools(registry: ToolRegistry, workspace: MemoryWorkspace) -> None:
    registry.register(_search_memory_tool(workspace))
    registry.register(_read_memory_tool(workspace))
    registry.register(_write_memory_tool(workspace))
    registry.register(_update_memory_tool(workspace))
    registry.register(_delete_memory_tool(workspace))


def _search_memory_tool(workspace: MemoryWorkspace) -> RegisteredTool:
    def search(params: dict[str, object]) -> ToolResult:
        try:
            result = workspace.search(params)
        except (TypeError, ValueError) as exc:
            return ToolResult(
                False,
                "memory_query_rejected",
                True,
                metrics={"error": str(exc)},
            )
        return ToolResult(True, "memory_search", False, metrics=result)

    return RegisteredTool(
        "search_memory",
        "Search durable scoped memory using bounded text, source, kind, subject, and spatial filters. Empty results are honest. Use live Body tools to recheck facts that may have changed.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 500},
                "kinds": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"type": "string", "enum": [item.value for item in MemoryKind]},
                },
                "sources": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {"type": "string", "enum": [item.value for item in MemorySource]},
                },
                "subject_key": {"type": "string", "maxLength": 256},
                "dimension": {"type": "string", "maxLength": 128},
                "center": _point_schema(),
                "radius": {"type": "number", "minimum": 0, "maximum": 1000000},
                "region": _region_schema(),
                "start": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
        search,
        _memory_sidecar("search_memory", permission="read_memory", tool_type="memory_query"),
    )


def _read_memory_tool(workspace: MemoryWorkspace) -> RegisteredTool:
    def read(params: dict[str, object]) -> ToolResult:
        memory_id = str(params.get("memory_id") or "")
        record = workspace.read(memory_id)
        if record is None:
            return ToolResult(
                False,
                "memory_not_found",
                False,
                metrics={"memory_id": memory_id},
            )
        return ToolResult(
            True,
            "memory_read",
            False,
            metrics=memory_record_payload(record),
        )

    return RegisteredTool(
        "read_memory",
        "Read one durable memory by stable memory_id after search_memory returns it.",
        {
            "type": "object",
            "properties": {"memory_id": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["memory_id"],
            "additionalProperties": False,
        },
        read,
        _memory_sidecar("read_memory", permission="read_memory", tool_type="memory_query"),
    )


def _write_memory_tool(workspace: MemoryWorkspace) -> RegisteredTool:
    def write(params: dict[str, object]) -> ToolResult:
        try:
            record = workspace.write(
                kind=MemoryKind(str(params.get("kind") or "")),
                source=MemorySource(str(params.get("source") or "")),
                title=str(params.get("title") or ""),
                content=str(params.get("content") or ""),
                subject_key=str(params.get("subject_key") or ""),
                evidence_ref=str(params.get("evidence_ref") or ""),
                dimension=str(params.get("dimension") or "") or None,
                point=_point(params.get("point")),
                region=_region(params.get("region")),
            )
        except (MemoryStateConflict, TypeError, ValueError) as exc:
            return ToolResult(
                False,
                "memory_write_rejected",
                True,
                metrics={"error": str(exc)},
            )
        return ToolResult(
            True,
            "memory_written",
            False,
            metrics=memory_record_payload(record),
        )

    return RegisteredTool(
        "write_memory",
        "Deliberately retain one durable fact or experience. Do not copy routine event logs. source=observed requires an authoritative tool/observation evidence_ref.",
        _memory_write_schema(require_all=True),
        write,
        _memory_sidecar("write_memory", permission="write_memory", tool_type="memory_write"),
    )


def _update_memory_tool(workspace: MemoryWorkspace) -> RegisteredTool:
    def update(params: dict[str, object]) -> ToolResult:
        memory_id = str(params.get("memory_id") or "")
        expected_revision = int(params.get("expected_revision") or 0)
        changes = {
            key: value
            for key, value in params.items()
            if key not in {"memory_id", "expected_revision"}
        }
        try:
            record = workspace.update(
                memory_id,
                expected_revision=expected_revision,
                changes=changes,
            )
        except (MemoryStateConflict, TypeError, ValueError) as exc:
            current = workspace.read(memory_id)
            return ToolResult(
                False,
                "memory_update_rejected",
                True,
                metrics={
                    "error": str(exc),
                    "current": None if current is None else memory_record_payload(current),
                },
            )
        return ToolResult(
            True,
            "memory_updated",
            False,
            metrics=memory_record_payload(record),
        )

    schema = _memory_write_schema(require_all=False)
    schema["properties"] = {
        "memory_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "expected_revision": {"type": "integer", "minimum": 1},
        **schema["properties"],
        "clear_geometry": {"type": "boolean"},
    }
    schema["required"] = ["memory_id", "expected_revision"]
    return RegisteredTool(
        "update_memory",
        "Update or correct one durable memory with optimistic revision checking. Lower-trust sources cannot overwrite higher-trust facts with the same identity.",
        schema,
        update,
        _memory_sidecar("update_memory", permission="write_memory", tool_type="memory_write"),
    )


def _delete_memory_tool(workspace: MemoryWorkspace) -> RegisteredTool:
    def delete(params: dict[str, object]) -> ToolResult:
        memory_id = str(params.get("memory_id") or "")
        expected_revision = int(params.get("expected_revision") or 0)
        try:
            workspace.delete(memory_id, expected_revision=expected_revision)
        except MemoryStateConflict as exc:
            return ToolResult(
                False,
                "memory_delete_rejected",
                True,
                metrics={"error": str(exc), "memory_id": memory_id},
            )
        return ToolResult(
            True,
            "memory_deleted",
            False,
            metrics={"memory_id": memory_id, "expected_revision": expected_revision},
        )

    return RegisteredTool(
        "delete_memory",
        "Delete one obsolete or consolidated memory by stable id and revision. This is deliberate reflection-time forgetting, not TTL expiry.",
        {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "expected_revision": {"type": "integer", "minimum": 1},
            },
            "required": ["memory_id", "expected_revision"],
            "additionalProperties": False,
        },
        delete,
        _memory_sidecar("delete_memory", permission="delete_memory", tool_type="memory_write"),
    )


def _memory_write_schema(*, require_all: bool) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [item.value for item in MemoryKind]},
            "source": {"type": "string", "enum": [item.value for item in MemorySource]},
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "content": {"type": "string", "minLength": 1, "maxLength": 12000},
            "subject_key": {"type": "string", "maxLength": 256},
            "evidence_ref": {"type": "string", "maxLength": 512},
            "dimension": {"type": "string", "maxLength": 128},
            "point": _point_schema(),
            "region": _region_schema(),
        },
        "additionalProperties": False,
    }
    if require_all:
        schema["required"] = ["kind", "source", "title", "content"]
    return schema


def _memory_sidecar(progress_key: str, *, permission: str, tool_type: str) -> ToolSidecar:
    return ToolSidecar(
        progress_key,
        mutating=False,
        source="agent.memory",
        tool_type=tool_type,
        permission=permission,
        body_scope=(),
        terminal_truth=("MemoryRecord.revision",),
    )


def _point_schema() -> dict[str, object]:
    return {
        "type": "array",
        "minItems": 3,
        "maxItems": 3,
        "items": {"type": "number"},
    }


def _region_schema() -> dict[str, object]:
    return {
        "type": "array",
        "minItems": 6,
        "maxItems": 6,
        "items": {"type": "number"},
    }


def _point(value: object) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("point must contain exactly 3 coordinates")
    return float(value[0]), float(value[1]), float(value[2])


def _region(value: object) -> tuple[float, float, float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        raise ValueError("region must contain exactly 6 bounds")
    return tuple(float(item) for item in value)  # type: ignore[return-value]


__all__ = ["MemoryWorkspace", "register_memory_tools"]
