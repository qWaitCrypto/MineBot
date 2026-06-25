#!/usr/bin/env python3
"""clear_furnace Body transaction e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import FurnaceTransactions, NavigationTransactions
from minebot.body.furnace import CLEAR_ORDER, FURNACE_SLOTS
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.navigation import SegmentedNavigator
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EClearFurnBot"
SKIP_EXIT_CODE = 77
FURNACE = (2, 59, 0)
NEAREST_FURNACE = (8, 59, 0)


def command(rcon: RconClient, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
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
        "time set day",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill 0 58 -2 4 62 2 air",
        "fill 0 58 -2 4 58 2 stone",
        f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} furnace",
        f"item replace block {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} container.0 with iron_ore 1",
        f"item replace block {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} container.1 with coal 1",
        f"item replace block {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} container.2 with iron_ingot 2",
    ]:
        command(rcon, cmd)


def reset_flat_world(rcon: RconClient) -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        "kill @e[type=!player]",
        "fill -2 59 -3 12 66 3 air",
        "fill -2 58 -3 12 58 3 stone",
        f"clear {BOT}",
        f"tp {BOT} 0 59 0 -90 0",
        f"item replace entity {BOT} hotbar.0 with air",
        f"item replace entity {BOT} hotbar.1 with air",
        f"item replace entity {BOT} hotbar.2 with air",
    ]:
        command(rcon, cmd)


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for z in range(z_min, z_max + 1)})


def make_runtime(body: ScarpetBody) -> FurnaceTransactions:
    policy = GovernancePolicy(natural_regions=[Region("furnace_nav", (-2, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(flat_world(-2, 12, -3, 3), NavigationCostModel(policy)),
    )
    return FurnaceTransactions(body, navigator=navigator, governance=policy)


def place_furnace_with_contents(rcon: RconClient, pos: tuple[int, int, int]) -> None:
    command(rcon, f"setblock {pos[0]} {pos[1]} {pos[2]} furnace")
    command(rcon, f"item replace block {pos[0]} {pos[1]} {pos[2]} container.0 with iron_ore 1")
    command(rcon, f"item replace block {pos[0]} {pos[1]} {pos[2]} container.1 with coal 1")
    command(rcon, f"item replace block {pos[0]} {pos[1]} {pos[2]} container.2 with iron_ingot 2")


def set_furnace_slot(rcon: RconClient, pos: tuple[int, int, int], slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set(l({pos[0]}, {pos[1]}, {pos[2]}), {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set(l({pos[0]}, {pos[1]}, {pos[2]}), {slot}, {count}, '{item}')")


def reset_direct_furnace(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 0 58 -2 4 62 2 air")
    command(rcon, "fill 0 58 -2 4 58 2 stone")
    command(rcon, f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} furnace")
    command(rcon, f"clear {BOT}")
    command(rcon, f"tp {BOT} 0 59 0")


def container_by_slot(body: ScarpetBody, pos: tuple[int, int, int]) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_container(pos, total_slots=3, page_size=3)}


def inventory_by_slot(body: ScarpetBody) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_inventory()}


def same_item(actual: str | None, expected: str | None) -> bool:
    if actual is None or expected is None:
        return actual == expected
    return actual == expected or actual == f"minecraft:{expected}" or f"minecraft:{actual}" == expected


def assert_furnace_clear_result(body: ScarpetBody, result, pos: tuple[int, int, int], furnace_before: dict[int, object]) -> dict[str, object]:
    expected_moves = [
        (slot_name, furnace_before[FURNACE_SLOTS[slot_name]])
        for slot_name in CLEAR_ORDER
        if not furnace_before[FURNACE_SLOTS[slot_name]].empty
    ]
    expected_moved_count = sum(slot.count for _, slot in expected_moves)
    furnace_after = container_by_slot(body, pos)
    inventory_after = inventory_by_slot(body)

    if not result.success or result.reason != "completed":
        raise AssertionError(f"clear_furnace failed: {result}")
    if int((result.metrics or {}).get("moved_count") or 0) != expected_moved_count:
        raise AssertionError(f"wrong moved count: {result.metrics}")
    executed = list((result.metrics or {}).get("executed") or [])
    if [step.get("furnace_slot") for step in executed] != [slot_name for slot_name, _ in expected_moves]:
        raise AssertionError(f"unexpected clear order: {executed}")
    if any(furnace_after[index].count != 0 for index in (0, 1, 2)):
        raise AssertionError(f"furnace not empty after clear: {furnace_after}")
    expected_by_slot = {slot_name: slot for slot_name, slot in expected_moves}
    for step in executed:
        furnace_slot = str(step.get("furnace_slot"))
        bot_slot = int(step["bot_slot"])
        expected = expected_by_slot[furnace_slot]
        received = inventory_after[bot_slot]
        if not same_item(received.item, expected.item) or received.count != expected.count:
            raise AssertionError(
                f"wrong {furnace_slot} receipt in bot slot {bot_slot}: "
                f"expected {expected}, got {received}"
            )
    return {
        "reason": result.reason,
        "moved_count": (result.metrics or {}).get("moved_count"),
        "clear_order": [step.get("furnace_slot") for step in executed],
        "received": {
            int(step["bot_slot"]): {
                "item": inventory_after[int(step["bot_slot"])].item,
                "count": inventory_after[int(step["bot_slot"])].count,
            }
            for step in executed
        },
    }


def run_direct_clear(body: ScarpetBody) -> dict[str, object]:
    runtime = FurnaceTransactions(body)
    furnace_before = container_by_slot(body, FURNACE)
    result = runtime.clear_furnace(FURNACE, timeout_s=6.0)
    return assert_furnace_clear_result(body, result, FURNACE, furnace_before)


def run_fuel_only_clear(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_furnace_slot(rcon, FURNACE, 0, None)
    set_furnace_slot(rcon, FURNACE, 1, "minecraft:coal", 4)
    set_furnace_slot(rcon, FURNACE, 2, None)
    runtime = FurnaceTransactions(body)
    furnace_before = container_by_slot(body, FURNACE)
    result = runtime.clear_furnace(FURNACE, timeout_s=6.0)
    summary = assert_furnace_clear_result(body, result, FURNACE, furnace_before)
    if summary["clear_order"] != ["fuel"]:
        raise AssertionError(f"fuel-only clear used wrong order: {summary}")
    fuel_receipt = next(iter(summary["received"].values()))
    if not same_item(fuel_receipt["item"], "minecraft:coal") or int(fuel_receipt["count"]) != 4:
        raise AssertionError(f"fuel-only clear did not receive coal x4: {summary}")
    return summary


def run_already_empty_clear(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    for slot in (0, 1, 2):
        set_furnace_slot(rcon, FURNACE, slot, None)
    runtime = FurnaceTransactions(body)
    before = container_by_slot(body, FURNACE)
    result = runtime.clear_furnace(FURNACE, timeout_s=6.0)
    after = container_by_slot(body, FURNACE)
    payload = result.to_payload()
    if not result.success or result.reason != "already_empty":
        raise AssertionError(f"empty furnace returned wrong truth: {payload}")
    if int((result.metrics or {}).get("occupied_furnace_slots", -1)) != 0:
        raise AssertionError(f"empty furnace reported occupied slots: {payload}")
    if (result.metrics or {}).get("moves") != []:
        raise AssertionError(f"empty furnace planned moves: {payload}")
    if any(not before[index].empty or not after[index].empty for index in (0, 1, 2)):
        raise AssertionError(f"empty furnace mutated slots: before={before} after={after} result={payload}")
    return {
        "reason": result.reason,
        "occupied_furnace_slots": result.metrics.get("occupied_furnace_slots"),
        "moves": result.metrics.get("moves"),
    }


def run_nearest_clear(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_flat_world(rcon)
    place_furnace_with_contents(rcon, NEAREST_FURNACE)
    runtime = make_runtime(body)
    furnace_before = container_by_slot(body, NEAREST_FURNACE)
    result = runtime.clear_nearest_furnace(search_radius=12, timeout_s=6.0, approach_timeout_s=18.0)
    payload = result.to_payload()
    summary = assert_furnace_clear_result(body, result, NEAREST_FURNACE, furnace_before)
    if result.metrics.get("furnace_target") != list(NEAREST_FURNACE):
        raise AssertionError(f"clear_nearest_furnace selected wrong target: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True:
        raise AssertionError(f"clear_nearest_furnace did not use shared navigation: {payload}")
    attempts = approach.get("attempts") or []
    if not attempts or attempts[-1].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"furnace approach navigation did not arrive: {payload}")
    final = body.get_state()
    if math.dist(final.pos, (NEAREST_FURNACE[0] + 0.5, NEAREST_FURNACE[1] + 0.5, NEAREST_FURNACE[2] + 0.5)) > 4.5:
        raise AssertionError(f"final body position is not in furnace interaction range: final={final.pos} result={payload}")
    summary["target"] = result.metrics.get("furnace_target")
    summary["navigation_reason"] = attempts[-1]["result"].get("reason")
    summary["final"] = final.pos
    return summary


def run_nearest_not_found(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_flat_world(rcon)
    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.clear_nearest_furnace(search_radius=6, timeout_s=6.0, approach_timeout_s=8.0)
    after = body.get_state()
    payload = result.to_payload()
    if result.success or result.reason != "furnace_not_found":
        raise AssertionError(f"clear_nearest_furnace not-found inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"clear_nearest_furnace not-found inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "before": before.pos, "after": after.pos}


def main() -> None:
    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (0, 59, 0))
        command(rcon, f"tp {BOT} 0 59 0")
        command(rcon, f"item replace entity {BOT} hotbar.0 with air")
        command(rcon, f"item replace entity {BOT} hotbar.1 with air")
        command(rcon, f"item replace entity {BOT} hotbar.2 with air")
        command(rcon, "script in minebot run minebot_reset()")

        cases = {
            "direct": lambda: run_direct_clear(body),
            "nearest": lambda: run_nearest_clear(rcon, body),
            "not_found": lambda: run_nearest_not_found(rcon, body),
            "fuel_only": lambda: run_fuel_only_clear(rcon, body),
            "already_empty": lambda: run_already_empty_clear(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_CLEAR_FURNACE_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown MINEBOT_CLEAR_FURNACE_CASES entries: {unknown}; valid={list(cases)}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
