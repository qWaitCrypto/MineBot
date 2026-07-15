#!/usr/bin/env python3
"""transfer_nearest_container e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import ContainerTransactions, NavigationTransactions
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.navigation import GridCell, GridWorld, NavigationCostModel, SegmentedNavigator
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EChestNavBot"
CHEST = (8, 59, 0)
BARREL = (8, 59, 1)
TRAPPED_CHEST = (8, 59, -1)
SKIP_EXIT_CODE = 77


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
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill -2 59 -3 12 66 3 air",
        "fill -2 58 -3 12 58 3 stone",
    ]:
        command(rcon, cmd)


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for z in range(z_min, z_max + 1)})


def make_runtime(body: ScarpetBody) -> ContainerTransactions:
    policy = GovernancePolicy(natural_regions=[Region("container_nav", (-2, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(flat_world(-2, 12, -3, 3), NavigationCostModel(policy)),
    )
    return ContainerTransactions(body, navigator=navigator, governance=policy)


def reset_world(rcon: RconClient, *, with_chest: bool) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill -2 59 -3 12 66 3 air")
    command(rcon, "fill -2 58 -3 12 58 3 stone")
    if with_chest:
        command(rcon, f"setblock {CHEST[0]} {CHEST[1]} {CHEST[2]} chest")
        command(rcon, f"item replace block {CHEST[0]} {CHEST[1]} {CHEST[2]} container.0 with diamond 3")
    command(rcon, f"clear {BOT}")
    command(rcon, f"tp {BOT} 0 59 0 -90 0")


def set_inventory_slot(rcon: RconClient, slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, {count}, '{item}')")


def set_container_slot(rcon: RconClient, pos: tuple[int, int, int], slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set(l({pos[0]}, {pos[1]}, {pos[2]}), {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set(l({pos[0]}, {pos[1]}, {pos[2]}), {slot}, {count}, '{item}')")


def inventory_count(body: ScarpetBody, item: str) -> int:
    slots = body.get_inventory()
    wanted = {item, f"minecraft:{item}"}
    return sum(slot.count for slot in slots if slot.item in wanted)


def inventory_by_slot(body: ScarpetBody) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_inventory()}


def same_item(actual: str | None, expected: str | None) -> bool:
    if actual is None or expected is None:
        return actual == expected
    return actual == expected or actual == f"minecraft:{expected}" or f"minecraft:{actual}" == expected


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_chest=True)
    runtime = make_runtime(body)
    before = inventory_count(body, "diamond")

    result = runtime.transfer_nearest_container(
        item="diamond",
        count=2,
        direction="container_to_bot",
        search_radius=12,
        total_slots=27,
        page_size=27,
        timeout_s=10.0,
        approach_timeout_s=18.0,
    )
    payload = result.to_payload()
    after = inventory_count(body, "diamond")
    final = body.get_state()
    chest_slots = {slot.slot: slot for slot in body.get_container(CHEST, total_slots=27, page_size=27)}

    if not result.success or result.reason != "completed":
        raise AssertionError(f"transfer_nearest_container happy path failed: {payload} final={final}")
    if result.metrics.get("container_target") != list(CHEST):
        raise AssertionError(f"transfer_nearest_container selected wrong target: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True:
        raise AssertionError(f"transfer_nearest_container did not use shared navigation: {payload}")
    attempts = approach.get("attempts") or []
    if not attempts or attempts[-1].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"container approach navigation did not arrive: {payload}")
    if after - before != 2:
        raise AssertionError(f"inventory diamond delta wrong: before={before} after={after} result={payload}")
    if chest_slots[0].count != 1:
        raise AssertionError(f"chest slot count wrong after transfer: slot0={chest_slots[0]} result={payload}")
    if math.dist(final.pos, (CHEST[0] + 0.5, CHEST[1] + 0.5, CHEST[2] + 0.5)) > 4.5:
        raise AssertionError(f"final body position is not in container interaction range: final={final.pos} result={payload}")

    return {
        "reason": result.reason,
        "target": result.metrics.get("container_target"),
        "before": before,
        "after": after,
        "final": final.pos,
        "navigation_reason": attempts[-1]["result"].get("reason"),
        "chest_slot0_count": chest_slots[0].count,
    }


def run_not_found_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_chest=False)
    runtime = make_runtime(body)
    before = body.get_state()

    result = runtime.transfer_nearest_container(
        item="diamond",
        count=1,
        direction="container_to_bot",
        search_radius=6,
        total_slots=27,
        page_size=27,
        timeout_s=8.0,
        approach_timeout_s=8.0,
    )
    after = body.get_state()
    payload = result.to_payload()
    if result.success or result.reason != "container_not_found":
        raise AssertionError(f"transfer_nearest_container not-found inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"transfer_nearest_container not-found inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "before": before.pos, "after": after.pos}


def run_merge_then_empty_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_chest=True)
    set_container_slot(rcon, CHEST, 0, "minecraft:oak_log", 63)
    set_container_slot(rcon, CHEST, 1, None)
    set_inventory_slot(rcon, 0, "minecraft:oak_log", 4)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = make_runtime(body)
    before_inventory = inventory_by_slot(body)
    before_chest = {slot.slot: slot for slot in body.get_container(CHEST, total_slots=27, page_size=27)}
    result = runtime.transfer_item(
        CHEST,
        item="minecraft:oak_log",
        count=4,
        direction="bot_to_container",
        total_slots=27,
        page_size=27,
        timeout_s=10.0,
    )
    payload = result.to_payload()
    after_inventory = inventory_by_slot(body)
    after_chest = {slot.slot: slot for slot in body.get_container(CHEST, total_slots=27, page_size=27)}

    if not result.success or result.reason != "completed":
        raise AssertionError(f"container merge-then-empty transfer failed: {payload}")
    if before_inventory[0].count != 4 or not same_item(before_inventory[0].item, "minecraft:oak_log"):
        raise AssertionError(f"unexpected bot setup slot 0: {before_inventory[0]}")
    if before_chest[0].count != 63 or not same_item(before_chest[0].item, "minecraft:oak_log") or not before_chest[1].empty:
        raise AssertionError(f"unexpected chest setup: slot0={before_chest[0]} slot1={before_chest[1]}")
    executed = list((result.metrics or {}).get("executed") or [])
    if [(step.get("source_slot"), step.get("dest_slot"), step.get("moved_count")) for step in executed] != [(0, 0, 1), (0, 1, 3)]:
        raise AssertionError(f"container merge-then-empty used wrong transfer plan: {payload}")
    if int((result.metrics or {}).get("moved_count") or 0) != 4:
        raise AssertionError(f"container merge-then-empty moved wrong count: {payload}")
    if after_inventory[0].count != 0:
        raise AssertionError(f"bot source slot not emptied: {after_inventory[0]} result={payload}")
    if after_chest[0].count != 64 or not same_item(after_chest[0].item, "minecraft:oak_log"):
        raise AssertionError(f"chest merge slot wrong: {after_chest[0]} result={payload}")
    if after_chest[1].count != 3 or not same_item(after_chest[1].item, "minecraft:oak_log"):
        raise AssertionError(f"chest empty slot fill wrong: {after_chest[1]} result={payload}")

    return {
        "reason": result.reason,
        "moved_count": result.metrics.get("moved_count"),
        "executed": [(step.get("source_slot"), step.get("dest_slot"), step.get("moved_count")) for step in executed],
        "chest_slot0_count": after_chest[0].count,
        "chest_slot1_count": after_chest[1].count,
        "bot_slot0_count": after_inventory[0].count,
    }


def run_partial_capacity_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_chest=True)
    set_container_slot(rcon, CHEST, 0, "minecraft:oak_log", 63)
    for slot in range(1, 27):
        set_container_slot(rcon, CHEST, slot, "minecraft:stone", 64)
    set_inventory_slot(rcon, 0, "minecraft:oak_log", 4)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = make_runtime(body)
    before_inventory = inventory_by_slot(body)
    before_chest = {slot.slot: slot for slot in body.get_container(CHEST, total_slots=27, page_size=27)}
    result = runtime.transfer_item(
        CHEST,
        item="minecraft:oak_log",
        count=4,
        direction="bot_to_container",
        total_slots=27,
        page_size=27,
        timeout_s=10.0,
    )
    payload = result.to_payload()
    after_inventory = inventory_by_slot(body)
    after_chest = {slot.slot: slot for slot in body.get_container(CHEST, total_slots=27, page_size=27)}

    if result.success or result.reason != "partial" or not result.can_retry:
        raise AssertionError(f"container partial-capacity transfer returned wrong result: {payload}")
    if before_inventory[0].count != 4 or not same_item(before_inventory[0].item, "minecraft:oak_log"):
        raise AssertionError(f"unexpected bot setup slot 0: {before_inventory[0]}")
    if before_chest[0].count != 63 or not same_item(before_chest[0].item, "minecraft:oak_log"):
        raise AssertionError(f"unexpected chest setup slot 0: {before_chest[0]}")
    if any(not same_item(before_chest[slot].item, "minecraft:stone") or before_chest[slot].count != 64 for slot in range(1, 27)):
        raise AssertionError(f"partial-capacity setup did not fill non-target slots: {before_chest}")
    if int((result.metrics or {}).get("planned_count") or 0) != 1:
        raise AssertionError(f"container partial-capacity planned wrong count: {payload}")
    if int((result.metrics or {}).get("moved_count") or 0) != 1:
        raise AssertionError(f"container partial-capacity moved wrong count: {payload}")
    executed = list((result.metrics or {}).get("executed") or [])
    if [(step.get("source_slot"), step.get("dest_slot"), step.get("moved_count")) for step in executed] != [(0, 0, 1)]:
        raise AssertionError(f"container partial-capacity used wrong transfer plan: {payload}")
    if after_inventory[0].count != 3 or not same_item(after_inventory[0].item, "minecraft:oak_log"):
        raise AssertionError(f"bot source slot did not retain remainder: {after_inventory[0]} result={payload}")
    if after_chest[0].count != 64 or not same_item(after_chest[0].item, "minecraft:oak_log"):
        raise AssertionError(f"chest partial merge slot wrong: {after_chest[0]} result={payload}")
    if any(not same_item(after_chest[slot].item, "minecraft:stone") or after_chest[slot].count != 64 for slot in range(1, 27)):
        raise AssertionError(f"partial-capacity mutated unrelated chest slots: {after_chest} result={payload}")

    return {
        "reason": result.reason,
        "planned_count": result.metrics.get("planned_count"),
        "moved_count": result.metrics.get("moved_count"),
        "executed": [(step.get("source_slot"), step.get("dest_slot"), step.get("moved_count")) for step in executed],
        "chest_slot0_count": after_chest[0].count,
        "bot_slot0_count": after_inventory[0].count,
        "can_retry": result.can_retry,
    }


def run_barrel_withdraw_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_chest=False)
    command(rcon, f"setblock {BARREL[0]} {BARREL[1]} {BARREL[2]} barrel")
    set_container_slot(rcon, BARREL, 0, "minecraft:emerald", 2)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = make_runtime(body)
    before = inventory_count(body, "emerald")
    result = runtime.transfer_nearest_container(
        item="minecraft:emerald",
        count=2,
        direction="container_to_bot",
        search_radius=12,
        total_slots=27,
        page_size=27,
        timeout_s=10.0,
        approach_timeout_s=18.0,
    )
    payload = result.to_payload()
    after = inventory_count(body, "emerald")
    barrel_slots = {slot.slot: slot for slot in body.get_container(BARREL, total_slots=27, page_size=27)}

    if not result.success or result.reason != "completed":
        raise AssertionError(f"barrel withdraw failed: {payload}")
    if result.metrics.get("container_target") != list(BARREL):
        raise AssertionError(f"barrel withdraw selected wrong target: {payload}")
    if result.metrics.get("container_type") not in {"barrel", "minecraft:barrel"}:
        raise AssertionError(f"barrel withdraw reported wrong container type: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True:
        raise AssertionError(f"barrel withdraw did not use shared navigation: {payload}")
    if after - before != 2:
        raise AssertionError(f"barrel withdraw bot inventory delta wrong: before={before} after={after} result={payload}")
    if barrel_slots[0].count != 0:
        raise AssertionError(f"barrel withdraw did not empty slot 0: {barrel_slots[0]} result={payload}")

    return {
        "reason": result.reason,
        "target": result.metrics.get("container_target"),
        "container_type": result.metrics.get("container_type"),
        "before": before,
        "after": after,
        "barrel_slot0_count": barrel_slots[0].count,
    }


def run_trapped_chest_deposit_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon, with_chest=False)
    command(rcon, f"setblock {TRAPPED_CHEST[0]} {TRAPPED_CHEST[1]} {TRAPPED_CHEST[2]} trapped_chest")
    set_container_slot(rcon, TRAPPED_CHEST, 0, None)
    set_inventory_slot(rcon, 0, "minecraft:redstone", 5)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = ContainerTransactions(
        body,
        governance=GovernancePolicy(natural_regions=[Region("trapped_chest", TRAPPED_CHEST, TRAPPED_CHEST)]),
    )
    before_inventory = inventory_by_slot(body)
    result = runtime.transfer_item(
        TRAPPED_CHEST,
        item="minecraft:redstone",
        count=5,
        direction="bot_to_container",
        total_slots=27,
        page_size=27,
        timeout_s=10.0,
    )
    payload = result.to_payload()
    after_inventory = inventory_by_slot(body)
    trapped_slots = {slot.slot: slot for slot in body.get_container(TRAPPED_CHEST, total_slots=27, page_size=27)}

    if not result.success or result.reason != "completed":
        raise AssertionError(f"trapped chest deposit failed: {payload}")
    if before_inventory[0].count != 5 or not same_item(before_inventory[0].item, "minecraft:redstone"):
        raise AssertionError(f"unexpected trapped chest deposit setup: {before_inventory[0]}")
    if after_inventory[0].count != 0:
        raise AssertionError(f"trapped chest deposit did not empty bot source slot: {after_inventory[0]} result={payload}")
    if trapped_slots[0].count != 5 or not same_item(trapped_slots[0].item, "minecraft:redstone"):
        raise AssertionError(f"trapped chest deposit did not write container slot 0: {trapped_slots[0]} result={payload}")
    if int((result.metrics or {}).get("moved_count") or 0) != 5:
        raise AssertionError(f"trapped chest deposit moved wrong count: {payload}")

    return {
        "reason": result.reason,
        "moved_count": result.metrics.get("moved_count"),
        "bot_slot0_count": after_inventory[0].count,
        "trapped_slot0": {"item": trapped_slots[0].item, "count": trapped_slots[0].count},
    }


def run_denied_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    policy: GovernancePolicy,
    expected_reason: str,
) -> dict[str, object]:
    reset_world(rcon, with_chest=True)
    set_container_slot(rcon, CHEST, 0, "minecraft:diamond", 3)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = ContainerTransactions(body, governance=policy)
    before_inventory = inventory_count(body, "diamond")
    before_chest = {slot.slot: slot for slot in body.get_container(CHEST, total_slots=27, page_size=27)}
    result = runtime.transfer_item(
        CHEST,
        item="minecraft:diamond",
        count=1,
        direction="container_to_bot",
        total_slots=27,
        page_size=27,
        timeout_s=10.0,
    )
    payload = result.to_payload()
    after_inventory = inventory_count(body, "diamond")
    after_chest = {slot.slot: slot for slot in body.get_container(CHEST, total_slots=27, page_size=27)}

    if result.success or result.reason != "container_denied":
        raise AssertionError(f"container governance inverse returned wrong result: {payload}")
    legality = result.metrics.get("legality") or {}
    if legality.get("reason") != expected_reason:
        raise AssertionError(f"container governance inverse returned wrong legality: {payload}")
    if before_inventory != after_inventory:
        raise AssertionError(f"container governance inverse mutated bot inventory: before={before_inventory} after={after_inventory} result={payload}")
    if before_chest[0].count != after_chest[0].count or not same_item(after_chest[0].item, "minecraft:diamond"):
        raise AssertionError(f"container governance inverse mutated chest: before={before_chest[0]} after={after_chest[0]} result={payload}")
    if "moves" in result.metrics:
        raise AssertionError(f"container governance inverse planned moves after denial: {payload}")

    return {
        "reason": result.reason,
        "legality_reason": legality.get("reason"),
        "chest_slot0_count": after_chest[0].count,
        "bot_diamond_count": after_inventory,
    }


def run_unknown_denied_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    return run_denied_path(
        rcon,
        body,
        policy=GovernancePolicy(),
        expected_reason="unknown_provenance",
    )


def run_protected_denied_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    return run_denied_path(
        rcon,
        body,
        policy=GovernancePolicy(protected_regions=[Region("protected_chest", CHEST, CHEST)]),
        expected_reason="protected_region",
    )


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
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")

        cases = {
            "happy": lambda: run_happy_path(rcon, body),
            "not_found": lambda: run_not_found_inverse(rcon, body),
            "merge_then_empty": lambda: run_merge_then_empty_path(rcon, body),
            "partial_capacity": lambda: run_partial_capacity_path(rcon, body),
            "barrel_withdraw": lambda: run_barrel_withdraw_path(rcon, body),
            "trapped_chest_deposit": lambda: run_trapped_chest_deposit_path(rcon, body),
            "unknown_denied": lambda: run_unknown_denied_path(rcon, body),
            "protected_denied": lambda: run_protected_denied_path(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_CONTAINER_NEAREST_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown MINEBOT_CONTAINER_NEAREST_CASES entries: {unknown}; valid={list(cases)}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
