"""Agent tools routed to the Java Body provider (``JavaBodyClient``).

This is the composition seam that lets the shared registry reach the
production-bound Java Body: `navigate_to` and `collect_block` are canonical,
implementation-neutral tools whose callables delegate to a
:class:`~minebot.game.java_body_adapter.JavaBodyClient`. The client answers
every mutation proposal through the injected production governance, so these
tools carry the same single-mutation-authority guarantee as the Scarpet path.

The tools return the Body :class:`ToolResult` the client already produces —
success reflects observed terminal truth (arrival / inventory delta), failures
keep the Java typed reason, and a mid-flight transport drop is reconciled by
the client, not relabeled here.
"""

from __future__ import annotations

from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import JsonObject, ToolResult
from minebot.game.java_body_adapter import JavaBodyClient


# What the Java Body's goal predicates accept, kept model-friendly: a target
# cell plus a goal kind. `interact` ends at any legal stand within reach of the
# target block (the stand-selection-unified-with-reachability goal); `near`
# ends within `range`; `xz` is a column goal for far targets.
_GOAL_KINDS = ("near", "interact", "xz")

STRING_LIST_SCHEMA = {"type": "array", "items": {"type": "string"}}


def register_java_body_tools(
    registry: ToolRegistry,
    client: JavaBodyClient,
    *,
    default_search_radius: int = 48,
) -> None:
    """Register the Java-Body-backed navigate/collect tools on ``registry``."""
    registry.register(_navigate_to_tool(client))
    registry.register(_collect_block_tool(client, default_search_radius))


def _ensure_connected(client: JavaBodyClient) -> ToolResult | None:
    """Lazy-connect so registry construction never blocks on the Body being up;
    an unreachable Java Body is a typed retryable failure, not a crash."""
    try:
        if not client.negotiated:
            client.connect()
        return None
    except Exception as error:  # noqa: BLE001 — transport-level unavailability
        return ToolResult(
            success=False,
            reason="java_body_unavailable",
            can_retry=True,
            metrics={"error": type(error).__name__},
        )


def _navigate_to_tool(client: JavaBodyClient) -> RegisteredTool:
    def run(params: JsonObject) -> ToolResult:
        kind = str(params.get("kind", "near"))
        if kind not in _GOAL_KINDS:
            return ToolResult(success=False, reason="invalid_goal_kind", can_retry=False)
        goal: dict = {"kind": kind, "x": int(params["x"]), "z": int(params["z"])}
        if kind != "xz":
            goal["y"] = int(params["y"])
        if "range" in params and params["range"] is not None:
            goal["range"] = float(params["range"])
        timeout_ticks = params.get("timeout_ticks")
        unavailable = _ensure_connected(client)
        if unavailable is not None:
            return unavailable
        try:
            return client.navigate(goal, timeout_ticks=int(timeout_ticks) if timeout_ticks is not None else None)
        except Exception as error:  # noqa: BLE001
            return ToolResult(success=False, reason="java_body_unavailable", can_retry=True,
                              metrics={"error": type(error).__name__})

    return RegisteredTool(
        "navigate_to",
        "Walk or swim to a target cell through the Java Body's own pathfinding, "
        "recovery, and honest terminal reporting. kind='near' ends within range "
        "of the cell; kind='interact' ends at a legal stand from which the target "
        "block can be reached; kind='xz' targets a column for far goals. Returns "
        "the observed terminal (arrived / no_path / stuck / timeout).",
        _object_schema(
            {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "z": {"type": "integer"},
                "kind": {"type": "string", "enum": list(_GOAL_KINDS)},
                "range": {"type": "number", "exclusiveMinimum": 0, "maximum": 64},
                "timeout_ticks": {"type": "integer", "minimum": 20, "maximum": 12000},
            },
            required=("x", "z"),
        ),
        run,
        ToolSidecar(
            "navigate_to",
            mutating=True,
            source="java_body",
            tool_type="navigation",
            permission="move",
            body_scope=("navigation",),
            terminal_truth=("position", "ToolResult"),
            timeout_s=180.0,
        ),
        projector=_navigation_projector,
    )


def _collect_block_tool(client: JavaBodyClient, default_radius: int) -> RegisteredTool:
    def run(params: JsonObject) -> ToolResult:
        block_types = tuple(str(item) for item in (params.get("block_types") or ()))
        if not block_types:
            return ToolResult(success=False, reason="no_block_types", can_retry=False)
        radius = int(params.get("search_radius", default_radius))
        timeout_ticks = params.get("timeout_ticks")
        unavailable = _ensure_connected(client)
        if unavailable is not None:
            return unavailable
        try:
            return client.collect_block(
                list(block_types),
                radius=radius,
                vertical_radius=int(params["vertical_radius"]) if params.get("vertical_radius") is not None else None,
                timeout_ticks=int(timeout_ticks) if timeout_ticks is not None else None,
            )
        except Exception as error:  # noqa: BLE001
            return ToolResult(success=False, reason="java_body_unavailable", can_retry=True,
                              metrics={"error": type(error).__name__})

    return RegisteredTool(
        "collect_block",
        "Collect one block of any of the requested types through the Java Body: "
        "it searches natural candidates, approaches a legal stand, proposes the "
        "break to governance, mines only on an explicit allow, and completes only "
        "on an authoritative inventory delta. A missing drop or a denied mutation "
        "is a typed failure, never a faked success.",
        _object_schema(
            {
                "block_types": STRING_LIST_SCHEMA,
                "search_radius": {"type": "integer", "minimum": 4, "maximum": 64},
                "vertical_radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "timeout_ticks": {"type": "integer", "minimum": 20, "maximum": 12000},
            },
            required=("block_types",),
        ),
        run,
        ToolSidecar(
            "collect_block",
            mutating=True,
            source="java_body",
            tool_type="collection",
            permission="break",
            body_scope=("navigation", "blocks", "inventory"),
            terminal_truth=("blockAt", "inventoryDelta", "ToolResult"),
            timeout_s=240.0,
        ),
        projector=_collect_projector,
    )


def _navigation_projector(reason: str, metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {"reason": reason}
    for key in ("replans", "elapsed_ticks", "expanded_nodes", "unloaded_touches"):
        if key in metrics:
            summary[key] = metrics[key]
    return summary


def _collect_projector(reason: str, metrics: dict[str, object]) -> JsonObject:
    summary: JsonObject = {"reason": reason}
    if "inventory_delta" in metrics:
        summary["inventory_delta"] = metrics["inventory_delta"]
    for key in ("candidates_tried", "replans"):
        if key in metrics:
            summary[key] = metrics[key]
    return summary


def _object_schema(properties: dict[str, object], *, required: tuple[str, ...] = ()) -> JsonObject:
    schema: JsonObject = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema
