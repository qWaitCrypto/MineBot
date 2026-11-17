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
# The client's builder refuses a larger cuboid (``invalid_fixture_size`` in
# MineBotClientBody.start); the tool says so before the Body is reserved.
BUILD_MAX_CELLS = 256


def _build_observation(reason, metrics):
    original = metrics.get("original_result", {})
    native = original.get("metrics", metrics)
    construction = native.get("construction") or {}
    handoff = metrics.get("direct_call_handoff", {})
    facts = handoff.get("facts", {})
    remaining = construction.get("remaining_cells")
    unknown = construction.get("unknown_cells")
    summary = {
        "action_id": native.get("action_id"),
        "native_reason": original.get("reason", reason),
        "terminal_source": native.get("terminal_source"),
        "native_stopped": native.get("native_stopped"),
        "construction": {
            key: value for key, value in construction.items()
            if key not in {"matched_positions", "remaining_cells", "unknown_cells"}
        },
        # Not measured is not zero: a refused or interrupted call may have no
        # construction reading at all.
        "remaining_cell_count": None if remaining is None else len(remaining),
        "remaining_sample": (remaining or [])[:8],
        "unknown_cell_count": None if unknown is None else len(unknown),
        "blocker": {key: value for key, value in facts.items() if key != "construction"},
        "settlement": handoff.get("settlement"),
    }
    summary["omitted_remaining_cell_count"] = max(0, len(remaining or []) - 8)
    return summary


def _build_too_large(params: dict) -> ToolResult | None:
    size = [int(value) for value in params["size"]]
    cells = size[0] * size[1] * size[2]
    if cells <= BUILD_MAX_CELLS:
        return None
    return ToolResult(
        False, "build_too_large", can_retry=False,
        next_suggestion=f"split it into cuboids of at most {BUILD_MAX_CELLS} blocks and build them one by one",
        metrics={"size": size, "requested_cells": cells, "max_cells": BUILD_MAX_CELLS, "native_started": False},
    )


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


def _cuboid_target(name: str, params: dict) -> dict | None:
    """The cuboid and block a build or clear leaves behind, in read_construction's terms."""
    if name == "buildStructure":
        return params
    if name == "clearArea":
        low = [min(params["start"][i], params["end"][i]) for i in range(3)]
        high = [max(params["start"][i], params["end"][i]) for i in range(3)]
        return {"anchor": low, "size": [high[i] - low[i] + 1 for i in range(3)], "block": "minecraft:air"}
    return None


def _execute(body: Body, name: str, params: dict, natural_regions: Sequence[object] = ()) -> ToolResult:
    if name == "buildStructure" and (too_large := _build_too_large(params)) is not None:
        return too_large
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
    # A clear reads its area like a build does: how much a call changed is a
    # world fact, also when it ends without success. A clear in a flooded pit
    # timed out twice after 5 minutes with no word that every broken cell had
    # filled with water (house run 2026-10-18).
    target = _cuboid_target(name, params)
    before = read_construction(body, target) if target is not None else None
    action = Action.create(name, params)
    action = action_with_timeout(action, float(params.get("timeout_s", 120)))
    result = body.execute(action)
    if not result.ok or not result.accepted:
        return ToolResult(False, result.error or "body_rejected", can_retry=True, metrics=dict(result.data or {}))
    terminal = terminal_event_to_tool_result(body.await_action_terminal(
        action.id, timeout_s=action_wait_timeout_s(action)))
    if target is not None and terminal.metrics.get("terminal_source") == "native":
        after = read_construction(body, target)
        partial = construction_delta(before or {}, after)
        verified = terminal.success and after["complete"] and after["matched_cells"] == after["expected_cells"]
        return ToolResult(
            verified,
            ("structure_verified" if name == "buildStructure" else "area_cleared") if verified
            else terminal.reason if not terminal.success
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


SET_MOVEMENT_DESCRIPTION = (
    "The Body's legs, a setting like set_walk_gaze. keep_moving: the Body walks or sprints from point to "
    "point inside a circle (away from drops, clouds and places that just hurt it) and goes on until you set "
    "stand, the Body dies, or it cannot move (you are then told where and why). Shots, eating and the shield "
    "happen while it walks; any other action (a walk, a dodge, placing, pillar_up...) holds the legs, and they "
    "go on by themselves when it ends. For fights where standing still is dangerous."
)


def _movement_words(movement: dict) -> str:
    if movement.get("mode") != "keep_moving":
        return "standing (your setting)"
    centre = [round(float(v)) for v in movement.get("center") or ()]
    return f"keeps moving around {centre} within {float(movement.get('radius') or 0):g} blocks (your setting)"


def _set_movement(body: Body, params: dict) -> ToolResult:
    """The Agent's leg mode (body-channels-20261105 §6.2; user 2026-11-06: 很多时候，失败都是因为它站在原地)."""
    setter = getattr(body, "set_movement", None)
    if not callable(setter):
        return ToolResult(False, "movement_unsupported", False)
    mode = str(params.get("mode") or "keep_moving")
    fields: dict = {"mode": mode}
    if mode == "keep_moving":
        center = params.get("center")
        if isinstance(center, (list, tuple)) and len(center) == 3:
            fields["center"] = [float(value) for value in center]
        fields["radius"] = float(params.get("radius", 12))
        fields["sprint"] = bool(params.get("sprint", True))
    try:
        terminal = setter(fields)
    except Exception as exc:
        return ToolResult(False, "movement_not_set", True, metrics={"message": str(exc)[:200]})
    movement = terminal.get("movement") if isinstance(terminal.get("movement"), dict) else {"mode": mode}
    return ToolResult(True, "movement_set", False, metrics={
        "movement": _movement_words(movement), "position": terminal.get("position")})


def _set_movement_tool(body: Body) -> RegisteredTool:
    return RegisteredTool(
        "set_movement", SET_MOVEMENT_DESCRIPTION,
        {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["keep_moving", "stand"]},
            "center": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                       "description": "Middle of the area; default: where the Body stands now."},
            "radius": {"type": "number", "minimum": 4, "maximum": 32, "default": 12},
            "sprint": {"type": "boolean", "default": True},
            "reason": {"type": "string", "description": "Why, in a few words."},
        }, "required": ["mode"], "additionalProperties": False},
        lambda params: _set_movement(body, params),
        ToolSidecar("set_movement", False, source="body.client", timeout_s=10),
        projector=lambda reason, metrics: dict(metrics),
    )


SET_WALK_GAZE_DESCRIPTION = (
    "Keep every walk of the Body looking at the ground (your calls, plans and Decision's own moves) "
    "until you turn it off or the Body dies; use it where looking around is risky, e.g. endermen in the End."
)
WALKING_LOOKS_DOWN = "looking down (your setting)"
WALKING_LOOKS_LEVEL = "level"


def _set_walk_gaze(body: Body, params: dict) -> ToolResult:
    """The Agent's standing walk gaze (user 2026-11-05: 死亡之后刷新): it stays
    until the Agent changes it or the Body dies; nothing else turns it on."""
    setter = getattr(body, "set_walk_look_down", None)
    if not callable(setter):
        return ToolResult(False, "walk_gaze_unsupported", False)
    on = bool(params["look_down"])
    was = setter(on, cause="agent")
    return ToolResult(True, "walk_gaze_set", False, metrics={
        "walking": WALKING_LOOKS_DOWN if on else WALKING_LOOKS_LEVEL,
        "changed": was != on,
    })


def _set_walk_gaze_tool(body: Body) -> RegisteredTool:
    return RegisteredTool(
        "set_walk_gaze", SET_WALK_GAZE_DESCRIPTION,
        {"type": "object", "properties": {
            "look_down": {"type": "boolean", "description": "true: walks look at the ground; false: walks look level."},
            "reason": {"type": "string", "description": "Why, in a few words."},
        }, "required": ["look_down"], "additionalProperties": False},
        lambda params: _set_walk_gaze(body, params),
        ToolSidecar("set_walk_gaze", False, source="body.client", timeout_s=5),
        projector=lambda reason, metrics: dict(metrics),
    )


def _process_state(body: Body) -> ToolResult:
    state = body.perceive("baritoneState", {})
    return ToolResult(state.ok, "process_state" if state.ok else state.error or "state_read_failed",
                      can_retry=not state.ok, metrics=dict(state.data))


def client_body_tools(body: Body, natural_regions: Sequence[object] = ()) -> tuple[RegisteredTool, ...]:
    specs = (
        ("build_structure", "buildStructure",
         f"Fill a cuboid with one block type; requires the material in inventory. At most {BUILD_MAX_CELLS} blocks "
         "per call (size x*y*z); build a larger area as several cuboids.",
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
            projector=_build_observation if name in {"build_structure", "clear_area"} else
                lambda reason, metrics: {key: bounded_summary_value(value) for key, value in metrics.items()},
            supervision_explanation=(
                "The builder normally navigates between declared target cells, selects available materials, "
                "places them, and returns a verified world result. Native progress and world evidence are authoritative."
            ) if name == "build_structure" else None,
            supervision_reader=(lambda params, _body=body: read_construction(_body, params))
            if name == "build_structure" else None,
        ))
    tools.append(_set_movement_tool(body))
    tools.append(_set_walk_gaze_tool(body))
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
