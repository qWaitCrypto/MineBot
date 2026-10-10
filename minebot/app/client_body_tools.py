"""Thin Agent bindings for the native tasks already validated on ClientBody.

Native resource mining is deliberately not bound here: ``collect_resource`` /
``collect_block_domain`` already run the client's ``mineResource`` behind the
harvest-tier gate and governance, so a raw entry would only be the same
process without them.
"""

from collections.abc import Sequence
from pathlib import Path
import time

import nbtlib
from minebot.contract import action_with_timeout, action_wait_timeout_s
from minebot.app.projection import bounded_summary_value
from minebot.app.construction_facts import read_construction, construction_delta

from minebot.brain.registry import RegisteredTool, ToolSidecar
from minebot.body.native_admission import work_volume_denial
from minebot.contract import Action, Body, ToolResult, execution_checkpoint, terminal_event_to_tool_result


_POS = {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3}
_TIMEOUT = {"type": "number", "exclusiveMinimum": 0, "maximum": 300, "default": 120}


def _build_observation(reason, metrics):
    original = metrics.get("original_result", {})
    native = original.get("metrics", metrics)
    construction = native.get("construction", {})
    handoff = metrics.get("direct_call_handoff", {})
    facts = handoff.get("facts", {})
    summary = {
        "action_id": native.get("action_id"),
        "native_reason": original.get("reason", reason),
        "terminal_source": native.get("terminal_source"),
        "native_stopped": native.get("native_stopped"),
        "construction": {
            key: value for key, value in construction.items()
            if key not in {"matched_positions", "remaining_cells", "unknown_cells"}
        },
        "remaining_cell_count": len(construction.get("remaining_cells", [])),
        "remaining_sample": construction.get("remaining_cells", [])[:8],
        "unknown_cell_count": len(construction.get("unknown_cells", [])),
        "blocker": {key: value for key, value in facts.items() if key != "construction"},
        "settlement": handoff.get("settlement"),
    }
    summary["omitted_remaining_cell_count"] = max(0, summary["remaining_cell_count"] - 8)
    return summary


def _work_volume(name: str, params: dict) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """The cuboid a native process is asked to change, when it declares one."""
    if name == "buildStructure":
        low = tuple(int(v) for v in params["anchor"])
        return low, tuple(low[i] + int(params["size"][i]) - 1 for i in range(3))
    if name == "clearArea":
        return tuple(int(v) for v in params["start"]), tuple(int(v) for v in params["end"])
    if name == "farmArea":
        x, y, z = (int(v) for v in params["anchor"])
        reach = int(params["range"])
        return (x - reach, y, z - reach), (x + reach, y, z + reach)
    return None


def _execute(body: Body, name: str, params: dict, natural_regions: Sequence[object] = ()) -> ToolResult:
    volume = _work_volume(name, params)
    if volume is not None:
        denied = work_volume_denial(natural_regions, *volume)
        if denied is not None:
            return denied
    if name in {"buildStructure", "clearArea", "farmArea", "builderBuildFile"}:
        state = body.perceive("baritoneState", {})
        if not state.ok:
            return ToolResult(False, state.error or "state_read_failed", can_retry=True)
        if state.data.get("disposable_world") is not True:
            return ToolResult(False, "native_mutation_requires_disposable_world", can_retry=False)
    before = read_construction(body, params) if name == "buildStructure" else None
    action = Action.create(name, params)
    action = action_with_timeout(action, float(params.get("timeout_s", 120)))
    result = body.execute(action)
    if not result.ok or not result.accepted:
        return ToolResult(False, result.error or "body_rejected", can_retry=True, metrics=dict(result.data or {}))
    terminal = terminal_event_to_tool_result(body.await_action_terminal(
        action.id, timeout_s=action_wait_timeout_s(action)))
    if name == "buildStructure" and terminal.metrics.get("terminal_source") == "native":
        after = read_construction(body, params)
        partial = construction_delta(before or {}, after)
        verified = terminal.success and after["complete"] and after["matched_cells"] == after["expected_cells"]
        return ToolResult(
            verified,
            "structure_verified" if verified else terminal.reason if not terminal.success
            else "world_verification_unavailable" if not after["complete"] else "world_mismatch",
            can_retry=not verified,
            metrics={**terminal.metrics, "construction": partial},
        )
    if terminal.success and name in {"buildStructure", "clearArea"}:
        if name == "buildStructure":
            low = params["anchor"]
            high = [low[i] + params["size"][i] - 1 for i in range(3)]
            block = params["block"]
            wanted = block if ":" in block else "minecraft:" + block
        else:
            low = [min(params["start"][i], params["end"][i]) for i in range(3)]
            high = [max(params["start"][i], params["end"][i]) for i in range(3)]
            wanted = "minecraft:air"
        cells = [([x, y, z], wanted) for x in range(low[0], high[0] + 1)
                 for y in range(low[1], high[1] + 1) for z in range(low[2], high[2] + 1)]
        return _verify_cells(body, cells, "structure_verified" if name == "buildStructure" else "area_cleared")
    return terminal


def _process_state(body: Body) -> ToolResult:
    state = body.perceive("baritoneState", {})
    return ToolResult(state.ok, "process_state" if state.ok else state.error or "state_read_failed",
                      can_retry=not state.ok, metrics=dict(state.data))


def client_body_tools(body: Body, natural_regions: Sequence[object] = ()) -> tuple[RegisteredTool, ...]:
    specs = (
        ("build_structure", "buildStructure", "Fill a cuboid with one block type; requires the material in inventory.",
         {"anchor": _POS, "size": {**_POS, "items": {"type": "integer", "minimum": 1}}, "block": {"type": "string"}}, ["anchor", "size", "block"]),
        ("clear_area", "clearArea", "Clear the inclusive cuboid between start and end.",
         {"start": _POS, "end": _POS}, ["start", "end"]),
        ("farm_area", "farmArea", "Harvest and replant every mature crop within range of anchor in one native pass; requires seeds and suitable tools. For one exact crop use harvest_and_resow; tilling and first sowing are till_farmland and sow_crop.",
         {"anchor": _POS, "range": {"type": "integer", "minimum": 1, "maximum": 64}}, ["anchor", "range"]),
        ("elytra_travel", "elytraTravel", "Fly to a landing target using equipped Elytra and rockets. Verified start is a drop from height.",
         {"target": _POS}, ["target"]),
    )
    tools = []
    for name, action, description, fields, required in specs:
        tools.append(RegisteredTool(
            name, description,
            {"type": "object", "properties": {**fields, "timeout_s": _TIMEOUT}, "required": required, "additionalProperties": False},
            lambda params, action=action: _execute(body, action, {"timeout_s": 120, **params}, natural_regions),
            ToolSidecar(name, True, source="body.client", body_scope=(action,), timeout_s=310),
            projector=_build_observation if name == "build_structure" else
                lambda reason, metrics: {key: bounded_summary_value(value) for key, value in metrics.items()},
        ))
    tools.extend((
        RegisteredTool("read_body_processes", "Read native running processes, paused builder, and current path state.",
                       {"type": "object", "properties": {}, "additionalProperties": False},
                       lambda params: _process_state(body), ToolSidecar("read_body_processes", False, source="body.client", timeout_s=15),
                       projector=lambda reason, metrics: dict(metrics)),
        RegisteredTool("build_schematic", "Build a local Sponge v2 .schem blueprint at origin and verify every solid/air cell. File must be accessible to both Python and the Minecraft client.",
                       {"type": "object", "properties": {"path": {"type": "string"}, "origin": _POS, "timeout_s": _TIMEOUT},
                        "required": ["path", "origin"], "additionalProperties": False},
                       lambda params: _build_schematic(body, params, natural_regions),
                       ToolSidecar("build_schematic", True, source="body.client", body_scope=("builderBuildFile",), timeout_s=310),
                       projector=lambda reason, metrics: dict(metrics)),
    ))
    return tuple(tools)


def _schematic_cells(path: Path, origin: list[int]) -> list[tuple[list[int], str]]:
    document = nbtlib.load(path)
    root = document.get("Schematic", document)
    if int(root.get("Version", 0)) != 2 or any(int(v) for v in root.get("Offset", [0, 0, 0])):
        raise ValueError("unsupported_schematic: expected Sponge v2 with zero offset")
    width, height, length = (int(root[key]) for key in ("Width", "Height", "Length"))
    if min(width, height, length) < 1:
        raise ValueError("invalid_schematic_dimensions")
    palette = {int(value): str(key) for key, value in root["Palette"].items()}
    data = bytes(int(v) & 255 for v in root["BlockData"])
    indices, value, shift = [], 0, 0
    for byte in data:
        value |= (byte & 127) << shift
        if byte & 128:
            shift += 7
            if shift > 28:
                raise ValueError("invalid_schematic_palette_index")
        else:
            indices.append(value)
            value, shift = 0, 0
    if shift or len(indices) != width * height * length:
        raise ValueError("invalid_schematic_block_data")
    return [([origin[0] + i % width, origin[1] + i // (width * length),
              origin[2] + i // width % length], palette[index]) for i, index in enumerate(indices)]


def _build_schematic(body: Body, params: dict, natural_regions: Sequence[object] = ()) -> ToolResult:
    path = Path(params["path"]).expanduser().resolve()
    try:
        cells = _schematic_cells(path, params["origin"])
    except (OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        return ToolResult(False, "schematic_read_failed", can_retry=False, metrics={"error": str(exc)[:400]})
    if cells:
        positions = [pos for pos, _block in cells]
        denied = work_volume_denial(
            natural_regions,
            tuple(min(pos[i] for pos in positions) for i in range(3)),
            tuple(max(pos[i] for pos in positions) for i in range(3)),
        )
        if denied is not None:
            return denied
    started = _execute(body, "builderBuildFile", {
        "path": str(path), "origin": params["origin"], "timeout_s": float(params.get("timeout_s", 120)),
    })
    if not started.success:
        return started
    deadline = time.monotonic() + float(params.get("timeout_s", 120))
    while True:
        execution_checkpoint()
        state = _process_state(body)
        if not state.success:
            body.interrupt("schematic_state_failed")
            return state
        if not state.metrics.get("builder_active"):
            break
        if time.monotonic() >= deadline:
            body.interrupt("schematic_deadline_exceeded")
            return ToolResult(False, "deadline_exceeded", can_retry=True, metrics={"process": state.metrics})
        time.sleep(0.1)
    return _verify_cells(body, cells, "schematic_verified")


def _verify_cells(body: Body, cells: list[tuple[list[int], str]], success_reason: str) -> ToolResult:
    mismatches, matched = [], 0
    for start in range(0, len(cells), 128):
        execution_checkpoint()
        batch = cells[start:start + 128]
        observed = body.perceive("blockCells", {"cells": [pos for pos, wanted in batch]})
        facts = observed.data.get("cells", [])
        if not observed.ok or not observed.complete or len(facts) != len(batch):
            return ToolResult(False, "world_verification_unavailable", can_retry=True, metrics={"matched_cells": matched})
        for (pos, wanted), fact in zip(batch, facts):
            block, _, properties = wanted.partition("[")
            expected = dict(entry.split("=", 1) for entry in properties.rstrip("]").split(",") if entry)
            if fact.get("type") == block and all(str(fact.get("properties", {}).get(k)) == v for k, v in expected.items()):
                matched += 1
            else:
                if len(mismatches) < 8:
                    mismatches.append({"pos": pos, "expected": wanted, "actual": fact})
    return ToolResult(matched == len(cells), success_reason if matched == len(cells) else "world_mismatch",
                      can_retry=matched != len(cells), metrics={"expected_cells": len(cells), "matched_cells": matched, "mismatches": mismatches})
