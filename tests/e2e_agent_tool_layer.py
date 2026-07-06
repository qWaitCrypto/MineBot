#!/usr/bin/env python3
"""Live proof for the formal Agent Phase-1 tool layer.

This intentionally bypasses the model. It proves the real-server harness tool
surface itself: manifest visibility, sidecar source/type metadata, governance /
precondition projection, and one executable Body tool through the SDK wrapper.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime  # noqa: E402
from minebot.app.runner import RuntimeRunContext  # noqa: E402
from minebot.brain.lifecycle import LifecycleState  # noqa: E402
from minebot.contract import Region  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402

BOT = "E2EAgentToolBot"
REGION = Region("agent-tool-layer", (-8, 0, -8), (16, 100, 8))


def command(rcon: RconClient, command_text: str, delay: float = 0.05) -> str:
    out = rcon.command(command_text)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon: RconClient) -> None:
    for cmd in [
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill -8 70 -8 16 76 8 air",
        "fill -8 69 -8 16 69 8 stone",
    ]:
        command(rcon, cmd)


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


async def invoke(tool, context: RuntimeRunContext, params: dict[str, object]) -> dict[str, object]:
    class Wrapper:
        def __init__(self, context: RuntimeRunContext) -> None:
            self.context = context

    return await tool.on_invoke_tool(Wrapper(context), json.dumps(params))


async def run_probe(body: ScarpetBody) -> dict[str, object]:
    parts = build_phase1_agent_runtime(
        body=body,
        goal_text="tool layer live probe",
        model_provider=None,
        config=Phase1RuntimeConfig(natural_region=REGION),
        agent_name="MineBotToolLayerE2E",
    )
    profile = parts.modes.profile_for(LifecycleState.ACTIVE)
    context = RuntimeRunContext(
        agent_context=parts.context,
        weld_context=parts.runtime.weld_context,
        profile=profile,
        tool_facts=parts.runtime.tool_facts,
        trace=parts.runtime.trace,
    )
    tools = {tool.name: tool for tool in parts.runtime.agent.tools}
    required = {
        "read_state",
        "read_inventory",
        "move_to",
        "search_for_block",
        "mine_block_collect",
        "collect_resource",
        "craft_item",
        "equip_item",
        "smelt_item",
    }
    missing = sorted(required - set(tools))
    if missing:
        raise AssertionError(f"formal tool layer missing tools: {missing}")

    state_result = await invoke(tools["read_state"], context, {})
    if not state_result.get("success") or state_result.get("reason") != "state_read":
        raise AssertionError(f"read_state failed: {state_result}")

    parts.runtime.set_tool_facts("move_to", {"precondition_missing": True})
    context.tool_facts = parts.runtime.tool_facts
    if tools["move_to"].is_enabled(type("Wrapper", (), {"context": context})(), parts.runtime.agent):
        raise AssertionError("move_to should be disabled by hard precondition")
    static_pos = body.get_state().pos

    parts.runtime.set_tool_facts("move_to", {})
    context.tool_facts = parts.runtime.tool_facts
    if not tools["move_to"].is_enabled(type("Wrapper", (), {"context": context})(), parts.runtime.agent):
        raise AssertionError("move_to should be enabled after precondition clears")
    target = (4, 70, 0)
    move_result = await invoke(tools["move_to"], context, {"pos": list(target), "timeout_s": 12.0})
    final_pos = body.get_state().pos
    dist = distance(final_pos, target)
    if not move_result.get("success") or dist > 1.0:
        raise AssertionError(f"move_to failed: result={move_result} final={final_pos} dist={dist:.3f}")

    _clear_inventory(body)
    _set_inventory_slot(body, 0, "minecraft:oak_planks", 4)
    before_craft = _inventory_counts(body)
    craft_result = await invoke(tools["craft_item"], context, {"item": "minecraft:crafting_table", "count": 1})
    after_craft = _inventory_counts(body)
    if not craft_result.get("success") or craft_result.get("reason") != "completed":
        raise AssertionError(f"craft_item failed: {craft_result}")
    if int(after_craft.get("crafting_table", 0)) - int(before_craft.get("crafting_table", 0)) != 1:
        raise AssertionError(f"craft_item did not produce one crafting table: before={before_craft} after={after_craft}")
    if int(before_craft.get("oak_planks", 0)) - int(after_craft.get("oak_planks", 0)) != 4:
        raise AssertionError(f"craft_item did not consume four planks: before={before_craft} after={after_craft}")

    _clear_inventory(body)
    before_missing = _inventory_counts(body)
    missing_result = await invoke(tools["craft_item"], context, {"item": "minecraft:crafting_table", "count": 1})
    after_missing = _inventory_counts(body)
    if missing_result.get("success") is not False or missing_result.get("reason") != "craft_plan_not_available":
        raise AssertionError(f"craft_item missing-material inverse returned wrong result: {missing_result}")
    if after_missing != before_missing:
        raise AssertionError(f"craft_item missing-material inverse changed inventory: before={before_missing} after={after_missing}")

    _clear_inventory(body)
    _set_inventory_slot(body, 9, "minecraft:iron_pickaxe", 1)
    before_equip = _inventory_counts(body)
    equip_result = await invoke(tools["equip_item"], context, {"item": "minecraft:iron_pickaxe", "target": "mainhand"})
    after_equip = _inventory_counts(body)
    if not equip_result.get("success") or equip_result.get("reason") != "completed":
        raise AssertionError(f"equip_item failed: {equip_result}")
    inventory_slots = {slot.slot: slot for slot in body.get_inventory()}
    if not any(
        index in inventory_slots
        and not inventory_slots[index].empty
        and str(inventory_slots[index].item).removeprefix("minecraft:") == "iron_pickaxe"
        for index in range(9)
    ):
        raise AssertionError(f"equip_item did not stage iron_pickaxe into the hotbar: slots={inventory_slots} result={equip_result}")
    if int(after_equip.get("iron_pickaxe", 0)) != int(before_equip.get("iron_pickaxe", 0)):
        raise AssertionError(f"equip_item changed item count: before={before_equip} after={after_equip}")

    _clear_inventory(body)
    before_missing_equip = _inventory_counts(body)
    missing_equip = await invoke(tools["equip_item"], context, {"item": "minecraft:iron_pickaxe", "target": "mainhand"})
    after_missing_equip = _inventory_counts(body)
    if missing_equip.get("success") is not False or missing_equip.get("reason") != "item_not_available":
        raise AssertionError(f"equip_item missing-material inverse returned wrong result: {missing_equip}")
    if after_missing_equip != before_missing_equip:
        raise AssertionError(
            f"equip_item missing-material inverse changed inventory: before={before_missing_equip} after={after_missing_equip}"
        )

    _clear_inventory(body)
    _set_inventory_slot(body, 0, "minecraft:furnace", 1)
    _set_inventory_slot(body, 1, "minecraft:raw_iron", 3)
    _set_inventory_slot(body, 2, "minecraft:oak_planks", 2)
    _set_inventory_slot(body, 3, None)
    before_smelt = _inventory_counts(body)
    smelt_result = await invoke(tools["smelt_item"], context, {"input_item": "minecraft:raw_iron", "count": 3})
    after_smelt = _inventory_counts(body)
    if not smelt_result.get("success") or smelt_result.get("reason") != "completed":
        raise AssertionError(f"smelt_item failed: {smelt_result} before={before_smelt} after={after_smelt}")
    if int(after_smelt.get("iron_ingot", 0)) - int(before_smelt.get("iron_ingot", 0)) != 3:
        raise AssertionError(f"smelt_item did not produce three iron ingots: before={before_smelt} after={after_smelt}")
    if int(after_smelt.get("furnace", 0)) != int(before_smelt.get("furnace", 0)):
        raise AssertionError(f"smelt_item did not reclaim carried furnace: before={before_smelt} after={after_smelt}")

    _clear_inventory(body)
    _set_inventory_slot(body, 0, "minecraft:raw_iron", 1)
    before_missing_fuel = _inventory_counts(body)
    missing_fuel = await invoke(tools["smelt_item"], context, {"input_item": "minecraft:raw_iron", "count": 1})
    after_missing_fuel = _inventory_counts(body)
    if missing_fuel.get("success") is not False or missing_fuel.get("reason") != "fuel_not_found":
        raise AssertionError(f"smelt_item missing-fuel inverse returned wrong result: {missing_fuel}")
    if after_missing_fuel != before_missing_fuel:
        raise AssertionError(f"smelt_item missing-fuel inverse changed inventory: before={before_missing_fuel} after={after_missing_fuel}")

    trace = parts.runtime.trace.snapshot()
    manifest = next((event for event in trace if event.get("event") == "tool_manifest"), None)
    if manifest is None:
        raise AssertionError(f"missing tool_manifest trace: {trace}")
    manifest_names = {row.get("name") for row in manifest.get("tools", []) if isinstance(row, dict)}
    if not required <= manifest_names:
        raise AssertionError(f"manifest incomplete: {manifest}")
    if not any(
        event.get("event") == "tool_enabled"
        and event.get("tool") == "move_to"
        and event.get("enabled") is False
        and event.get("source") == "body.navigation"
        and event.get("tool_type") == "navigation"
        for event in trace
    ):
        raise AssertionError(f"missing disabled move_to metadata trace: {trace}")
    if not any(
        event.get("event") == "tool_result" and event.get("tool") == "move_to" and event.get("success") is True
        for event in trace
    ):
        raise AssertionError(f"missing executable move_to result trace: {trace}")
    return {
        "manifest": sorted(manifest_names),
        "static_pos": static_pos,
        "final_pos": final_pos,
        "dist": round(dist, 3),
        "craft_delta": {
            item: int(after_craft.get(item, 0)) - int(before_craft.get(item, 0))
            for item in sorted(set(before_craft) | set(after_craft))
        },
        "missing_craft_reason": missing_result.get("reason"),
        "equip_reason": equip_result.get("reason"),
        "missing_equip_reason": missing_equip.get("reason"),
        "smelt_delta": {
            item: int(after_smelt.get(item, 0)) - int(before_smelt.get(item, 0))
            for item in sorted(set(before_smelt) | set(after_smelt))
        },
        "missing_smelt_fuel_reason": missing_fuel.get("reason"),
        "events": [event.get("event") for event in trace],
    }


def _set_inventory_slot(body: ScarpetBody, slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        body.transport.request(f"script in minebot run inventory_set('{body.bot_name}', {slot}, 0)")
        return
    body.transport.request(f"script in minebot run inventory_set('{body.bot_name}', {slot}, {count}, '{item}')")


def _clear_inventory(body: ScarpetBody) -> None:
    body.transport.request(f"clear {body.bot_name}")
    for slot in range(46):
        _set_inventory_slot(body, slot, None)


def _inventory_counts(body: ScarpetBody) -> dict[str, int]:
    counts: dict[str, int] = {}
    start: int | None = 0
    while start is not None:
        perception = body.perceive("inventory", {"start": start, "limit": 12})
        if not perception.ok:
            raise AssertionError(f"inventory perception failed: {perception}")
        for row in perception.data.get("slots") or []:
            if not isinstance(row, dict) or row.get("empty"):
                continue
            item = str(row.get("item") or "").removeprefix("minecraft:")
            if item:
                counts[item] = counts.get(item, 0) + int(row.get("count") or 0)
        next_start = perception.data.get("nextStart")
        start = int(next_start) if next_start is not None else None
    return counts


def main() -> None:
    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (0, 70, 0))
        command(rcon, f"tp {BOT} 0 70 0 -90 0")
        print(asyncio.run(run_probe(body)))


if __name__ == "__main__":
    main()
