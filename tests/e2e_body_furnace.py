#!/usr/bin/env python3
"""furnaceTransfer e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, FurnaceTransactions, NavigationTransactions
from minebot.game import GovernancePolicy, GridCell, GridWorld, NavigationCostModel, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.navigation import SegmentedNavigator
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EFurnaceBot"
SKIP_EXIT_CODE = 77
FURNACE = (2, 59, 0)
NEAREST_FURNACE = (8, 59, 0)
TEMP_FURNACE = (2, 59, 1)
AUTO_TEMP_FURNACE = (0, 59, -1)


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
        f"item replace block {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} container.2 with iron_ingot 2",
    ]:
        command(rcon, cmd)


def set_furnace_slot(rcon: RconClient, pos: tuple[int, int, int], slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set(l({pos[0]}, {pos[1]}, {pos[2]}), {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set(l({pos[0]}, {pos[1]}, {pos[2]}), {slot}, {count}, '{item}')")


def set_inventory_slot(rcon: RconClient, slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, {count}, '{item}')")


def reset_direct_furnace(rcon: RconClient) -> None:
    command(rcon, "script in minebot run minebot_reset()")
    command(rcon, "fill 0 58 -2 4 62 2 air")
    command(rcon, "fill 0 58 -2 4 58 2 stone")
    command(rcon, f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} furnace")
    for slot in (0, 1, 2):
        set_furnace_slot(rcon, FURNACE, slot, None)
    command(rcon, f"clear {BOT}")
    command(rcon, f"tp {BOT} 0 59 0")
    for slot in range(46):
        set_inventory_slot(rcon, slot, None)


def reset_flat_world(rcon: RconClient) -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        "kill @e[type=!player]",
        "fill -2 59 -3 12 66 3 air",
        "fill -2 58 -3 12 58 3 stone",
        f"clear {BOT}",
        f"tp {BOT} 0 59 0 -90 0",
    ]:
        command(rcon, cmd)
    for slot in range(46):
        set_inventory_slot(rcon, slot, None)


def flat_world(x_min: int, x_max: int, z_min: int, z_max: int, *, y: int = 59) -> GridWorld:
    return GridWorld({(x, y, z): GridCell() for x in range(x_min, x_max + 1) for z in range(z_min, z_max + 1)})


def make_runtime(body: ScarpetBody) -> FurnaceTransactions:
    policy = GovernancePolicy(natural_regions=[Region("furnace_nav", (-2, 0, -3), (12, 100, 3))])
    navigator = NavigationTransactions(
        body,
        SegmentedNavigator(flat_world(-2, 12, -3, 3), NavigationCostModel(policy)),
    )
    return FurnaceTransactions(body, navigator=navigator, governance=policy)


def container_by_slot(body: ScarpetBody, pos: tuple[int, int, int]) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_container(pos, total_slots=3, page_size=3)}


def inventory_by_slot(body: ScarpetBody) -> dict[int, object]:
    return {slot.slot: slot for slot in body.get_inventory()}


def same_item(actual: str | None, expected: str | None) -> bool:
    if actual is None or expected is None:
        return actual == expected
    return actual == expected or actual == f"minecraft:{expected}" or f"minecraft:{actual}" == expected


def assert_empty_furnace_and_preserved_slots(
    body: ScarpetBody,
    result_payload: dict[str, object],
    expected_inventory: tuple[tuple[int, str, int], ...],
) -> None:
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if not after_furnace[0].empty or not after_furnace[1].empty or not after_furnace[2].empty:
        raise AssertionError(f"preflight failure mutated furnace: furnace={after_furnace} result={result_payload}")
    for slot_index, expected_item, expected_count in expected_inventory:
        slot = after_inventory[slot_index]
        if not same_item(slot.item, expected_item) or slot.count != expected_count:
            raise AssertionError(
                f"preflight failure lost slot {slot_index}: inventory={after_inventory} result={result_payload}"
            )


def run_transfer_output_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_furnace_slot(rcon, FURNACE, 2, "minecraft:iron_ingot", 2)
    set_inventory_slot(rcon, 3, None)
    command(rcon, "script in minebot run minebot_reset()")

    event = body.furnace_transfer(
        pos=FURNACE,
        direction="furnace_to_bot",
        furnace_slot="output",
        bot_slot=3,
    )
    if event.name != "furnaceDone" or not event.data.get("success"):
        raise AssertionError(f"furnaceTransfer failed: {event}")
    if event.data.get("item") not in {"iron_ingot", "minecraft:iron_ingot"}:
        raise AssertionError(f"wrong transferred item: {event.data}")
    if int(event.data.get("count") or 0) != 2:
        raise AssertionError(f"wrong transferred count: {event.data}")
    if event.data.get("furnace_slot") != "output" or int(event.data.get("furnace_slot_index") or -1) != 2:
        raise AssertionError(f"wrong furnace slot facts: {event.data}")

    return {
        "event": event.name,
        "item": event.data.get("item"),
        "count": event.data.get("count"),
        "slot": event.data.get("furnace_slot"),
    }


def run_smelt_once_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=18.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"smelt_once failed: {payload} furnace={after_furnace} inventory={after_inventory}")
    if not same_item(after_inventory[2].item, "minecraft:iron_ingot") or after_inventory[2].count != 1:
        raise AssertionError(f"smelt_once did not collect output: slot2={after_inventory[2]} result={payload}")
    if not after_furnace[0].empty or not after_furnace[2].empty:
        raise AssertionError(f"smelt_once left input/output in furnace: furnace={after_furnace} result={payload}")
    executed = result.metrics.get("executed") or []
    if [step.get("kind") for step in executed] != ["deposit_input", "deposit_fuel", "collect_output"]:
        raise AssertionError(f"smelt_once executed wrong lifecycle steps: {payload}")
    if not result.metrics.get("polls"):
        raise AssertionError(f"smelt_once did not expose output polls: {payload}")
    return {
        "reason": result.reason,
        "executed": [step.get("kind") for step in executed],
        "poll_count": len(result.metrics.get("polls") or []),
        "output_slot": {"item": after_inventory[2].item, "count": after_inventory[2].count},
    }


def run_smelt_two_items_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 2)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=2,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=2,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=26.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"two-item smelt failed: {payload} furnace={after_furnace} inventory={after_inventory}")
    if not same_item(after_inventory[2].item, "minecraft:iron_ingot") or after_inventory[2].count != 2:
        raise AssertionError(f"two-item smelt did not collect full output: slot2={after_inventory[2]} result={payload}")
    if not after_furnace[0].empty or not after_furnace[2].empty:
        raise AssertionError(f"two-item smelt left input/output in furnace: furnace={after_furnace} result={payload}")
    executed = result.metrics.get("executed") or []
    input_move = executed[0].get("result") if executed else {}
    output_move = executed[-1].get("result") if executed else {}
    if ((input_move.get("metrics") or {}).get("count") != 2) or ((output_move.get("metrics") or {}).get("count") != 2):
        raise AssertionError(f"two-item smelt did not move exact counts: {payload}")
    return {
        "reason": result.reason,
        "poll_count": len(result.metrics.get("polls") or []),
        "input_moved": (input_move.get("metrics") or {}).get("count"),
        "output_moved": (output_move.get("metrics") or {}).get("count"),
        "output_slot": {"item": after_inventory[2].item, "count": after_inventory[2].count},
    }


def run_smelt_blast_furnace_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    command(rcon, f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} blast_furnace")
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=12.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"blast_furnace smelt failed: {payload} furnace={after_furnace} inventory={after_inventory}")
    executed = result.metrics.get("executed") or []
    transfer_type = ((executed[0].get("result") or {}).get("metrics") or {}).get("furnace_type") if executed else None
    if transfer_type not in {"blast_furnace", "minecraft:blast_furnace"}:
        raise AssertionError(f"blast_furnace smelt reported wrong furnace type: {payload}")
    if not same_item(after_inventory[2].item, "minecraft:iron_ingot") or after_inventory[2].count != 1:
        raise AssertionError(f"blast_furnace smelt did not collect output: slot2={after_inventory[2]} result={payload}")
    if not after_furnace[0].empty or not after_furnace[2].empty:
        raise AssertionError(f"blast_furnace smelt left input/output in furnace: furnace={after_furnace} result={payload}")
    return {
        "reason": result.reason,
        "furnace_type": transfer_type,
        "output_slot": {"item": after_inventory[2].item, "count": after_inventory[2].count},
    }


def run_smelt_smoker_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    command(rcon, f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} smoker")
    set_inventory_slot(rcon, 0, "minecraft:porkchop", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:porkchop",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:cooked_porkchop",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=12.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"smoker smelt failed: {payload} furnace={after_furnace} inventory={after_inventory}")
    executed = result.metrics.get("executed") or []
    transfer_type = ((executed[0].get("result") or {}).get("metrics") or {}).get("furnace_type") if executed else None
    if transfer_type not in {"smoker", "minecraft:smoker"}:
        raise AssertionError(f"smoker smelt reported wrong furnace type: {payload}")
    if not same_item(after_inventory[2].item, "minecraft:cooked_porkchop") or after_inventory[2].count != 1:
        raise AssertionError(f"smoker smelt did not collect output: slot2={after_inventory[2]} result={payload}")
    if not after_furnace[0].empty or not after_furnace[2].empty:
        raise AssertionError(f"smoker smelt left input/output in furnace: furnace={after_furnace} result={payload}")
    return {
        "reason": result.reason,
        "furnace_type": transfer_type,
        "output_slot": {"item": after_inventory[2].item, "count": after_inventory[2].count},
    }


def run_smelt_wrong_recipe_timeout_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    furnace_type: str,
    input_item: str,
    output_item: str,
) -> dict[str, object]:
    reset_direct_furnace(rcon)
    command(rcon, f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} {furnace_type}")
    set_inventory_slot(rcon, 0, input_item, 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    set_inventory_slot(rcon, 3, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item=input_item,
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item=output_item,
        output_count=1,
        output_slot=2,
        poll_interval_s=0.2,
        smelt_timeout_s=1.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "smelt_timeout":
        raise AssertionError(f"wrong-recipe timeout returned wrong truth: furnace_type={furnace_type} result={payload}")
    if not after_furnace[0].empty or not after_furnace[1].empty or not after_furnace[2].empty:
        raise AssertionError(
            f"wrong-recipe timeout did not reclaim furnace slots: furnace_type={furnace_type} furnace={after_furnace} result={payload}"
        )
    if not any(same_item(slot.item, input_item) and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"wrong-recipe timeout lost input item: furnace_type={furnace_type} inventory={after_inventory} result={payload}"
        )
    if not any(same_item(slot.item, "minecraft:coal") and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"wrong-recipe timeout unexpectedly burned fuel: furnace_type={furnace_type} inventory={after_inventory} result={payload}"
        )
    if any(same_item(slot.item, output_item) and slot.count > 0 for slot in after_inventory.values()):
        raise AssertionError(
            f"wrong-recipe timeout produced unexpected output: furnace_type={furnace_type} inventory={after_inventory} result={payload}"
        )
    reclaim = result.metrics.get("reclaim") or []
    if len(reclaim) < 2 or reclaim[0].get("furnace_slot") != "input" or reclaim[1].get("furnace_slot") != "fuel":
        raise AssertionError(f"wrong-recipe timeout exposed wrong reclaim order: furnace_type={furnace_type} result={payload}")
    fuel_reclaim = reclaim[1].get("result") or {}
    if fuel_reclaim.get("reason") != "completed":
        raise AssertionError(f"wrong-recipe timeout did not reclaim unburned fuel: furnace_type={furnace_type} result={payload}")
    executed = result.metrics.get("executed") or []
    deposit_type = ((executed[0].get("result") or {}).get("metrics") or {}).get("furnace_type") if executed else None
    if deposit_type not in {furnace_type, f"minecraft:{furnace_type}"}:
        raise AssertionError(f"wrong-recipe timeout reported wrong furnace type: furnace_type={furnace_type} result={payload}")
    return {
        "reason": result.reason,
        "furnace_type": deposit_type,
        "fuel_reclaim_reason": fuel_reclaim.get("reason"),
    }


def run_smelt_furnace_not_empty_path(rcon: RconClient, body: ScarpetBody, occupied_slot: str) -> dict[str, object]:
    slot_index_by_name = {"input": 0, "fuel": 1, "output": 2}
    item_by_slot = {
        "input": "minecraft:gold_ore",
        "fuel": "minecraft:coal",
        "output": "minecraft:iron_ingot",
    }
    reset_direct_furnace(rcon)
    slot_index = slot_index_by_name[occupied_slot]
    occupied_item = item_by_slot[occupied_slot]
    set_furnace_slot(rcon, FURNACE, slot_index, occupied_item, 1)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "smelt_furnace_not_empty":
        raise AssertionError(f"non-empty furnace preflight returned wrong truth: slot={occupied_slot} result={payload}")
    if result.metrics.get("occupied_slot") != occupied_slot:
        raise AssertionError(f"non-empty furnace reported wrong occupied slot: slot={occupied_slot} result={payload}")
    occupied = after_furnace[slot_index]
    if not same_item(occupied.item, occupied_item) or occupied.count != 1:
        raise AssertionError(f"non-empty furnace preflight mutated occupied slot: furnace={after_furnace} result={payload}")
    for slot_index, expected in ((0, "minecraft:raw_iron"), (1, "minecraft:coal")):
        slot = after_inventory[slot_index]
        if not same_item(slot.item, expected) or slot.count != 1:
            raise AssertionError(f"non-empty furnace preflight lost inventory slot {slot_index}: inventory={after_inventory} result={payload}")
    return {
        "reason": result.reason,
        "occupied_slot": result.metrics.get("occupied_slot"),
        "slot_item": occupied.item,
    }


def run_smelt_timeout_reclaim_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:cobblestone", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:cobblestone",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.2,
        smelt_timeout_s=1.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "smelt_timeout":
        raise AssertionError(f"smelt timeout inverse returned wrong truth: {payload}")
    if not after_furnace[0].empty or not after_furnace[2].empty:
        raise AssertionError(f"smelt timeout did not reclaim input/output: furnace={after_furnace} result={payload}")
    if not any(same_item(slot.item, "minecraft:cobblestone") and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(f"smelt timeout lost input cobblestone: inventory={after_inventory} result={payload}")
    return {
        "reason": result.reason,
        "poll_count": len(result.metrics.get("polls") or []),
        "reclaim": result.metrics.get("reclaim"),
    }


def run_smelt_partial_timeout_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 2)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    set_inventory_slot(rcon, 3, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=2,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=2,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=12.5,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "smelt_partial_timeout":
        raise AssertionError(f"partial smelt timeout returned wrong truth: {payload}")
    partial = result.metrics.get("partial_output") or {}
    partial_result = partial.get("result") or {}
    partial_metrics = partial_result.get("metrics") or {}
    if partial_result.get("reason") != "completed" or int(partial_metrics.get("count") or 0) < 1:
        raise AssertionError(f"partial smelt did not collect produced output: {payload}")
    if not any(same_item(slot.item, "minecraft:iron_ingot") and slot.count >= 1 for slot in after_inventory.values()):
        raise AssertionError(f"partial smelt lost produced output: inventory={after_inventory} result={payload}")
    if not after_furnace[2].empty:
        raise AssertionError(f"partial smelt left output in furnace after collection: furnace={after_furnace} result={payload}")
    return {
        "reason": result.reason,
        "partial_count": partial_metrics.get("count"),
        "poll_count": len(result.metrics.get("polls") or []),
        "reclaim": result.metrics.get("reclaim"),
    }


def run_smelt_auto_fuel_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:bamboo", 4)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:bamboo",
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=18.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"auto-fuel smelt failed: {payload} furnace={after_furnace} inventory={after_inventory}")
    fuel = result.metrics.get("fuel") or {}
    if fuel.get("count") != 4 or not fuel.get("auto"):
        raise AssertionError(f"auto-fuel smelt planned wrong fuel budget: {payload}")
    if not same_item(after_inventory[2].item, "minecraft:iron_ingot") or after_inventory[2].count != 1:
        raise AssertionError(f"auto-fuel smelt did not collect output: slot2={after_inventory[2]} result={payload}")
    if not after_furnace[0].empty or not after_furnace[1].empty or not after_furnace[2].empty:
        raise AssertionError(f"auto-fuel smelt left furnace slots occupied: furnace={after_furnace} result={payload}")
    return {
        "reason": result.reason,
        "fuel_count": fuel.get("count"),
        "fuel_auto": fuel.get("auto"),
        "output_slot": {"item": after_inventory[2].item, "count": after_inventory[2].count},
    }


def run_smelt_auto_fuel_insufficient_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:bamboo", 3)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:bamboo",
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "smelt_fuel_not_available":
        raise AssertionError(f"auto-fuel insufficient inverse returned wrong truth: {payload}")
    if not after_furnace[0].empty or not after_furnace[1].empty or not after_furnace[2].empty:
        raise AssertionError(f"auto-fuel insufficient mutated furnace: furnace={after_furnace} result={payload}")
    if not same_item(after_inventory[0].item, "minecraft:raw_iron") or after_inventory[0].count != 1:
        raise AssertionError(f"auto-fuel insufficient lost input: inventory={after_inventory} result={payload}")
    if not same_item(after_inventory[1].item, "minecraft:bamboo") or after_inventory[1].count != 3:
        raise AssertionError(f"auto-fuel insufficient lost fuel: inventory={after_inventory} result={payload}")
    return {
        "reason": result.reason,
        "planned_fuel": result.metrics.get("fuel_count"),
        "available_fuel": result.metrics.get("available_count"),
    }


def run_smelt_auto_stick_fuel_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:stick", 2)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:stick",
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=18.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, FURNACE)
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"auto-stick-fuel smelt failed: {payload} furnace={after_furnace} inventory={after_inventory}")
    fuel = result.metrics.get("fuel") or {}
    if fuel.get("count") != 2 or not fuel.get("auto"):
        raise AssertionError(f"auto-stick-fuel planned wrong fuel budget: {payload}")
    if not same_item(after_inventory[2].item, "minecraft:iron_ingot") or after_inventory[2].count != 1:
        raise AssertionError(f"auto-stick-fuel smelt did not collect output: slot2={after_inventory[2]} result={payload}")
    if not after_furnace[0].empty or not after_furnace[1].empty or not after_furnace[2].empty:
        raise AssertionError(f"auto-stick-fuel smelt left furnace slots occupied: furnace={after_furnace} result={payload}")
    return {
        "reason": result.reason,
        "fuel_count": fuel.get("count"),
        "fuel_auto": fuel.get("auto"),
        "seconds_available": fuel.get("seconds_available"),
        "output_slot": {"item": after_inventory[2].item, "count": after_inventory[2].count},
    }


def run_smelt_input_missing_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:coal", 1)
    set_inventory_slot(rcon, 1, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=1,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    if result.success or result.reason != "smelt_input_not_available":
        raise AssertionError(f"input-missing preflight returned wrong truth: {payload}")
    assert_empty_furnace_and_preserved_slots(body, payload, ((0, "minecraft:coal", 1),))
    return {"reason": result.reason, "input_item": result.metrics.get("input_item")}


def run_smelt_output_no_space_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    for slot in range(46):
        set_inventory_slot(rcon, slot, "minecraft:stone", 64)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    if result.success or result.reason != "smelt_output_no_space":
        raise AssertionError(f"output-no-space preflight returned wrong truth: {payload}")
    assert_empty_furnace_and_preserved_slots(
        body,
        payload,
        ((0, "minecraft:raw_iron", 1), (1, "minecraft:coal", 1), (2, "minecraft:stone", 64)),
    )
    return {"reason": result.reason, "output_item": result.metrics.get("output_item")}


def run_smelt_unknown_auto_fuel_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:iron_nugget", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body)
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:iron_nugget",
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    if result.success or result.reason != "smelt_unknown_fuel_value":
        raise AssertionError(f"unknown-auto-fuel preflight returned wrong truth: {payload}")
    assert_empty_furnace_and_preserved_slots(
        body,
        payload,
        ((0, "minecraft:raw_iron", 1), (1, "minecraft:iron_nugget", 1)),
    )
    return {"reason": result.reason, "fuel_item": result.metrics.get("fuel_item")}


def run_smelt_governance_unknown_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(body, governance=GovernancePolicy())
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    if result.success or result.reason != "furnace_denied":
        raise AssertionError(f"smelt governance unknown returned wrong truth: {payload}")
    legality = result.metrics.get("legality") or {}
    if legality.get("reason") != "unknown_provenance":
        raise AssertionError(f"smelt governance unknown reported wrong legality: {payload}")
    assert_empty_furnace_and_preserved_slots(
        body,
        payload,
        ((0, "minecraft:raw_iron", 1), (1, "minecraft:coal", 1)),
    )
    return {"reason": result.reason, "legality": legality}


def run_smelt_governance_protected_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_direct_furnace(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = FurnaceTransactions(
        body,
        governance=GovernancePolicy(
            protected_regions=[Region("protected_furnace", FURNACE, FURNACE)],
        ),
    )
    result = runtime.smelt_once(
        FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
    )
    payload = result.to_payload()
    if result.success or result.reason != "furnace_denied":
        raise AssertionError(f"smelt governance protected returned wrong truth: {payload}")
    legality = result.metrics.get("legality") or {}
    if legality.get("reason") != "protected_region":
        raise AssertionError(f"smelt governance protected reported wrong legality: {payload}")
    assert_empty_furnace_and_preserved_slots(
        body,
        payload,
        ((0, "minecraft:raw_iron", 1), (1, "minecraft:coal", 1)),
    )
    return {"reason": result.reason, "legality": legality}


def run_smelt_nearest_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_flat_world(rcon)
    command(rcon, f"setblock {NEAREST_FURNACE[0]} {NEAREST_FURNACE[1]} {NEAREST_FURNACE[2]} furnace")
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = make_runtime(body)
    result = runtime.smelt_nearest_furnace(
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        search_radius=12,
        poll_interval_s=0.5,
        smelt_timeout_s=18.0,
        transfer_timeout_s=6.0,
        approach_timeout_s=18.0,
    )
    payload = result.to_payload()
    after_furnace = container_by_slot(body, NEAREST_FURNACE)
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"nearest smelt failed: {payload} furnace={after_furnace} inventory={after_inventory}")
    if result.metrics.get("furnace_target") != list(NEAREST_FURNACE):
        raise AssertionError(f"nearest smelt selected wrong target: {payload}")
    approach = result.metrics.get("approach") or {}
    if approach.get("navigated") is not True:
        raise AssertionError(f"nearest smelt did not use shared navigation: {payload}")
    attempts = approach.get("attempts") or []
    if not attempts or attempts[-1].get("result", {}).get("reason") != "arrived":
        raise AssertionError(f"nearest smelt approach did not arrive: {payload}")
    if not same_item(after_inventory[2].item, "minecraft:iron_ingot") or after_inventory[2].count != 1:
        raise AssertionError(f"nearest smelt did not collect output: slot2={after_inventory[2]} result={payload}")
    if not after_furnace[0].empty or not after_furnace[2].empty:
        raise AssertionError(f"nearest smelt left input/output in furnace: furnace={after_furnace} result={payload}")
    final = body.get_state()
    if math.dist(final.pos, (NEAREST_FURNACE[0] + 0.5, NEAREST_FURNACE[1] + 0.5, NEAREST_FURNACE[2] + 0.5)) > 4.5:
        raise AssertionError(f"nearest smelt final position outside interaction range: final={final.pos} result={payload}")
    return {
        "reason": result.reason,
        "target": result.metrics.get("furnace_target"),
        "navigation_reason": attempts[-1]["result"].get("reason"),
        "output_slot": {"item": after_inventory[2].item, "count": after_inventory[2].count},
    }


def run_smelt_nearest_not_found_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 1, "minecraft:coal", 1)
    set_inventory_slot(rcon, 2, None)
    command(rcon, "script in minebot run minebot_reset()")

    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.smelt_nearest_furnace(
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=2,
        search_radius=6,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
        approach_timeout_s=8.0,
    )
    after = body.get_state()
    after_inventory = inventory_by_slot(body)
    payload = result.to_payload()
    if result.success or result.reason != "furnace_not_found":
        raise AssertionError(f"nearest smelt not-found inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"nearest smelt not-found moved the body: before={before.pos} after={after.pos} result={payload}")
    if not same_item(after_inventory[0].item, "minecraft:raw_iron") or after_inventory[0].count != 1:
        raise AssertionError(f"nearest smelt not-found lost input: inventory={after_inventory} result={payload}")
    if not same_item(after_inventory[1].item, "minecraft:coal") or after_inventory[1].count != 1:
        raise AssertionError(f"nearest smelt not-found lost fuel: inventory={after_inventory} result={payload}")
    return {"reason": result.reason, "can_retry": result.can_retry, "before": before.pos, "after": after.pos}


def run_smelt_temporary_furnace_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    input_count: int = 1,
    output_count: int = 1,
    smelt_timeout_s: float = 18.0,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, "minecraft:furnace", 1)
    set_inventory_slot(rcon, 1, "minecraft:raw_iron", input_count)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {TEMP_FURNACE[0]} {TEMP_FURNACE[1]} {TEMP_FURNACE[2]} air")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_temporary_furnace(
        TEMP_FURNACE,
        input_item="minecraft:raw_iron",
        input_count=input_count,
        fuel_item="minecraft:coal",
        output_item="minecraft:iron_ingot",
        output_count=output_count,
        output_slot=3,
        poll_interval_s=0.5,
        smelt_timeout_s=smelt_timeout_s,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": TEMP_FURNACE[0], "y": TEMP_FURNACE[1], "z": TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"temporary furnace smelt failed: {payload} block_after={block_after.data} inventory={after_inventory}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(f"temporary furnace was not reclaimed: block_after={block_after.data} result={payload}")
    if not same_item(after_inventory[3].item, "minecraft:iron_ingot") or after_inventory[3].count != output_count:
        raise AssertionError(f"temporary furnace smelt did not collect output: slot3={after_inventory[3]} result={payload}")
    smelt = result.metrics.get("smelt") or {}
    executed = (smelt.get("metrics") or {}).get("executed") or []
    input_move = (executed[0].get("result") or {}) if executed else {}
    output_move = (executed[-1].get("result") or {}) if executed else {}
    if ((input_move.get("metrics") or {}).get("count") != input_count) or (
        (output_move.get("metrics") or {}).get("count") != output_count
    ):
        raise AssertionError(
            f"temporary furnace did not move exact counts: input_count={input_count} output_count={output_count} result={payload}"
        )
    reclaim = result.metrics.get("reclaim") or {}
    if reclaim.get("reason") not in {"completed", "already_clear"}:
        raise AssertionError(f"temporary furnace reclaim did not complete: {payload}")
    return {
        "reason": result.reason,
        "input_moved": (input_move.get("metrics") or {}).get("count"),
        "output_moved": (output_move.get("metrics") or {}).get("count"),
        "block_after": block_after.data,
        "output_slot": {"item": after_inventory[3].item, "count": after_inventory[3].count},
        "reclaim_reason": reclaim.get("reason"),
    }


def run_smelt_temporary_furnace_partial_timeout_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, "minecraft:furnace", 1)
    set_inventory_slot(rcon, 1, "minecraft:raw_iron", 2)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {TEMP_FURNACE[0]} {TEMP_FURNACE[1]} {TEMP_FURNACE[2]} air")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_temporary_furnace(
        TEMP_FURNACE,
        input_item="minecraft:raw_iron",
        input_count=2,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=2,
        output_slot=3,
        poll_interval_s=0.5,
        smelt_timeout_s=12.5,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": TEMP_FURNACE[0], "y": TEMP_FURNACE[1], "z": TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_smelt_failed:smelt_partial_timeout":
        raise AssertionError(f"temporary furnace partial timeout returned wrong truth: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(f"temporary furnace partial timeout did not reclaim furnace: block_after={block_after.data} result={payload}")
    if not any(same_item(slot.item, "minecraft:iron_ingot") and slot.count >= 1 for slot in after_inventory.values()):
        raise AssertionError(f"temporary furnace partial timeout lost produced output: inventory={after_inventory} result={payload}")
    smelt = result.metrics.get("smelt") or {}
    partial = (smelt.get("metrics") or {}).get("partial_output") or {}
    partial_result = partial.get("result") or {}
    partial_metrics = partial_result.get("metrics") or {}
    if partial_result.get("reason") != "completed" or int(partial_metrics.get("count") or 0) < 1:
        raise AssertionError(f"temporary furnace partial timeout did not collect partial output: {payload}")
    reclaim_steps = (smelt.get("metrics") or {}).get("reclaim") or []
    if not reclaim_steps or reclaim_steps[0].get("furnace_slot") != "input":
        raise AssertionError(f"temporary furnace partial timeout did not expose input reclaim: {payload}")
    reclaim = result.metrics.get("reclaim") or {}
    if reclaim.get("reason") not in {"completed", "already_clear"}:
        raise AssertionError(f"temporary furnace partial timeout did not reclaim furnace: {payload}")
    return {
        "reason": result.reason,
        "partial_count": partial_metrics.get("count"),
        "output_slot": {"item": after_inventory[3].item, "count": after_inventory[3].count},
        "input_reclaim": reclaim_steps[0],
        "furnace_reclaim_reason": reclaim.get("reason"),
    }


def run_smelt_temporary_special_furnace_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    furnace_item: str,
    input_item: str,
    output_item: str,
    input_count: int = 1,
    output_count: int = 1,
    smelt_timeout_s: float = 18.0,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, furnace_item, 1)
    set_inventory_slot(rcon, 1, input_item, input_count)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {TEMP_FURNACE[0]} {TEMP_FURNACE[1]} {TEMP_FURNACE[2]} air")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_temporary_furnace(
        TEMP_FURNACE,
        input_item=input_item,
        input_count=input_count,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item=output_item,
        output_count=output_count,
        output_slot=3,
        furnace_item=furnace_item,
        poll_interval_s=0.5,
        smelt_timeout_s=smelt_timeout_s,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": TEMP_FURNACE[0], "y": TEMP_FURNACE[1], "z": TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(
            f"temporary special furnace smelt failed: furnace_item={furnace_item} payload={payload} block_after={block_after.data} inventory={after_inventory}"
        )
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(
            f"temporary special furnace was not reclaimed: furnace_item={furnace_item} block_after={block_after.data} result={payload}"
        )
    if not same_item(after_inventory[3].item, output_item) or after_inventory[3].count != output_count:
        raise AssertionError(
            f"temporary special furnace did not collect output: furnace_item={furnace_item} slot3={after_inventory[3]} result={payload}"
        )
    place = result.metrics.get("place") or {}
    placed_type = place.get("metrics", {}).get("block_type")
    if placed_type not in {furnace_item, f"minecraft:{furnace_item}"}:
        raise AssertionError(
            f"temporary special furnace placed wrong block type: furnace_item={furnace_item} placed_type={placed_type} result={payload}"
        )
    smelt = result.metrics.get("smelt") or {}
    executed = (smelt.get("metrics") or {}).get("executed") or []
    transfer_type = ((executed[0].get("result") or {}).get("metrics") or {}).get("furnace_type") if executed else None
    if transfer_type not in {furnace_item.removeprefix("minecraft:"), furnace_item}:
        raise AssertionError(
            f"temporary special furnace reported wrong smelt type: furnace_item={furnace_item} transfer_type={transfer_type} result={payload}"
        )
    input_move = (executed[0].get("result") or {}) if executed else {}
    output_move = (executed[-1].get("result") or {}) if executed else {}
    if ((input_move.get("metrics") or {}).get("count") != input_count) or (
        (output_move.get("metrics") or {}).get("count") != output_count
    ):
        raise AssertionError(
            f"temporary special furnace did not move exact counts: furnace_item={furnace_item} input_count={input_count} output_count={output_count} result={payload}"
        )
    reclaim = result.metrics.get("reclaim") or {}
    if reclaim.get("reason") not in {"completed", "already_clear"}:
        raise AssertionError(
            f"temporary special furnace reclaim did not complete: furnace_item={furnace_item} result={payload}"
        )
    return {
        "reason": result.reason,
        "placed_type": placed_type,
        "smelt_type": transfer_type,
        "input_moved": (input_move.get("metrics") or {}).get("count"),
        "output_moved": (output_move.get("metrics") or {}).get("count"),
        "output_slot": {"item": after_inventory[3].item, "count": after_inventory[3].count},
        "reclaim_reason": reclaim.get("reason"),
    }


def run_smelt_temporary_special_furnace_partial_timeout_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    furnace_item: str,
    input_item: str,
    output_item: str,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, furnace_item, 1)
    set_inventory_slot(rcon, 1, input_item, 2)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {TEMP_FURNACE[0]} {TEMP_FURNACE[1]} {TEMP_FURNACE[2]} air")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_temporary_furnace(
        TEMP_FURNACE,
        input_item=input_item,
        input_count=2,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item=output_item,
        output_count=2,
        output_slot=3,
        furnace_item=furnace_item,
        poll_interval_s=0.5,
        smelt_timeout_s=6.5,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": TEMP_FURNACE[0], "y": TEMP_FURNACE[1], "z": TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_smelt_failed:smelt_partial_timeout":
        raise AssertionError(f"temporary special furnace partial timeout returned wrong truth: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(
            f"temporary special furnace partial timeout did not reclaim furnace: block_after={block_after.data} result={payload}"
        )
    if not any(same_item(slot.item, output_item) and slot.count >= 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"temporary special furnace partial timeout lost produced output: furnace_item={furnace_item} inventory={after_inventory} result={payload}"
        )
    smelt = result.metrics.get("smelt") or {}
    partial = (smelt.get("metrics") or {}).get("partial_output") or {}
    partial_result = partial.get("result") or {}
    partial_metrics = partial_result.get("metrics") or {}
    if partial_result.get("reason") != "completed" or int(partial_metrics.get("count") or 0) < 1:
        raise AssertionError(f"temporary special furnace partial timeout did not collect partial output: {payload}")
    reclaim_steps = (smelt.get("metrics") or {}).get("reclaim") or []
    if not reclaim_steps or reclaim_steps[0].get("furnace_slot") != "input":
        raise AssertionError(f"temporary special furnace partial timeout did not expose input reclaim: {payload}")
    reclaim = result.metrics.get("reclaim") or {}
    if reclaim.get("reason") not in {"completed", "already_clear"}:
        raise AssertionError(f"temporary special furnace partial timeout did not reclaim furnace: {payload}")
    return {
        "reason": result.reason,
        "partial_count": partial_metrics.get("count"),
        "output_slot": {"item": after_inventory[3].item, "count": after_inventory[3].count},
        "input_reclaim": reclaim_steps[0],
        "furnace_reclaim_reason": reclaim.get("reason"),
    }


def run_smelt_temporary_furnace_smelt_timeout_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, "minecraft:furnace", 1)
    set_inventory_slot(rcon, 1, "minecraft:cobblestone", 1)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {TEMP_FURNACE[0]} {TEMP_FURNACE[1]} {TEMP_FURNACE[2]} air")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_temporary_furnace(
        TEMP_FURNACE,
        input_item="minecraft:cobblestone",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=3,
        poll_interval_s=0.2,
        smelt_timeout_s=1.0,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": TEMP_FURNACE[0], "y": TEMP_FURNACE[1], "z": TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_smelt_failed:smelt_timeout":
        raise AssertionError(f"temporary furnace smelt-timeout returned wrong truth: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(f"temporary furnace smelt-timeout did not reclaim furnace: block_after={block_after.data} result={payload}")
    if not any(same_item(slot.item, "minecraft:cobblestone") and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(f"temporary furnace smelt-timeout lost input: inventory={after_inventory} result={payload}")
    if any(same_item(slot.item, "minecraft:iron_ingot") and slot.count > 0 for slot in after_inventory.values()):
        raise AssertionError(f"temporary furnace smelt-timeout produced unexpected output: inventory={after_inventory} result={payload}")
    smelt = result.metrics.get("smelt") or {}
    reclaim_steps = (smelt.get("metrics") or {}).get("reclaim") or []
    if not reclaim_steps or reclaim_steps[0].get("furnace_slot") != "input":
        raise AssertionError(f"temporary furnace smelt-timeout did not expose input reclaim: {payload}")
    if len(reclaim_steps) < 2 or reclaim_steps[1].get("reason") != "already_empty":
        raise AssertionError(f"temporary furnace smelt-timeout did not expose consumed fuel truth: {payload}")
    reclaim = result.metrics.get("reclaim") or {}
    if reclaim.get("reason") not in {"completed", "already_clear"}:
        raise AssertionError(f"temporary furnace smelt-timeout did not complete furnace reclaim: {payload}")
    return {
        "reason": result.reason,
        "block_after": block_after.data,
        "input_reclaim": reclaim_steps[0],
        "fuel_reclaim_reason": reclaim_steps[1].get("reason"),
        "furnace_reclaim_reason": reclaim.get("reason"),
    }


def run_smelt_temporary_special_furnace_wrong_recipe_timeout_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    furnace_item: str,
    input_item: str,
    output_item: str,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, furnace_item, 1)
    set_inventory_slot(rcon, 1, input_item, 1)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {TEMP_FURNACE[0]} {TEMP_FURNACE[1]} {TEMP_FURNACE[2]} air")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_temporary_furnace(
        TEMP_FURNACE,
        input_item=input_item,
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item=output_item,
        output_count=1,
        output_slot=3,
        furnace_item=furnace_item,
        poll_interval_s=0.2,
        smelt_timeout_s=1.0,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": TEMP_FURNACE[0], "y": TEMP_FURNACE[1], "z": TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_smelt_failed:smelt_timeout":
        raise AssertionError(f"temporary special furnace wrong-recipe timeout returned wrong truth: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(
            f"temporary special furnace wrong-recipe timeout did not reclaim furnace: block_after={block_after.data} result={payload}"
        )
    if not any(same_item(slot.item, input_item) and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"temporary special furnace wrong-recipe timeout lost input: inventory={after_inventory} result={payload}"
        )
    if not any(same_item(slot.item, "minecraft:coal") and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"temporary special furnace wrong-recipe timeout lost unburned fuel: inventory={after_inventory} result={payload}"
        )
    if any(same_item(slot.item, output_item) and slot.count > 0 for slot in after_inventory.values()):
        raise AssertionError(
            f"temporary special furnace wrong-recipe timeout produced unexpected output: inventory={after_inventory} result={payload}"
        )
    place = result.metrics.get("place") or {}
    if place.get("metrics", {}).get("block_type") not in {furnace_item, f"minecraft:{furnace_item}"}:
        raise AssertionError(f"temporary special furnace wrong-recipe timeout placed wrong block type: {payload}")
    smelt = result.metrics.get("smelt") or {}
    reclaim_steps = (smelt.get("metrics") or {}).get("reclaim") or []
    if not reclaim_steps or reclaim_steps[0].get("furnace_slot") != "input":
        raise AssertionError(f"temporary special furnace wrong-recipe timeout did not expose input reclaim: {payload}")
    fuel_reclaim = reclaim_steps[1].get("result") if len(reclaim_steps) > 1 else None
    if not fuel_reclaim or fuel_reclaim.get("reason") != "completed":
        raise AssertionError(f"temporary special furnace wrong-recipe timeout did not reclaim unburned fuel: {payload}")
    reclaim = result.metrics.get("reclaim") or {}
    if reclaim.get("reason") not in {"completed", "already_clear"}:
        raise AssertionError(f"temporary special furnace wrong-recipe timeout did not reclaim furnace: {payload}")
    return {
        "reason": result.reason,
        "placed_type": place.get("metrics", {}).get("block_type"),
        "fuel_reclaim_reason": fuel_reclaim.get("reason"),
        "furnace_reclaim_reason": reclaim.get("reason"),
    }


def run_smelt_temporary_furnace_occupied_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, "minecraft:furnace", 1)
    set_inventory_slot(rcon, 1, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {TEMP_FURNACE[0]} {TEMP_FURNACE[1]} {TEMP_FURNACE[2]} stone")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_temporary_furnace(
        TEMP_FURNACE,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=3,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": TEMP_FURNACE[0], "y": TEMP_FURNACE[1], "z": TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_place_failed:place_denied:target_occupied":
        raise AssertionError(f"temporary furnace occupied inverse returned wrong truth: {payload}")
    if block_after.data.get("type") not in {"stone", "minecraft:stone"}:
        raise AssertionError(f"temporary furnace occupied inverse changed target block: block_after={block_after.data} result={payload}")
    for slot_index, expected in ((0, "minecraft:furnace"), (1, "minecraft:raw_iron"), (2, "minecraft:coal")):
        slot = after_inventory[slot_index]
        if not same_item(slot.item, expected) or slot.count != 1:
            raise AssertionError(f"temporary furnace occupied inverse lost slot {slot_index}: inventory={after_inventory} result={payload}")
    return {"reason": result.reason, "block_after": block_after.data}


def run_smelt_temporary_furnace_auto_site_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    input_count: int = 1,
    output_count: int = 1,
    smelt_timeout_s: float = 18.0,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, "minecraft:furnace", 1)
    set_inventory_slot(rcon, 1, "minecraft:raw_iron", input_count)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1]} {AUTO_TEMP_FURNACE[2]} air")
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1] - 1} {AUTO_TEMP_FURNACE[2]} stone")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp_auto", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_nearby_temporary_furnace(
        input_item="minecraft:raw_iron",
        input_count=input_count,
        fuel_item="minecraft:coal",
        output_item="minecraft:iron_ingot",
        output_count=output_count,
        output_slot=3,
        radius=1,
        poll_interval_s=0.5,
        smelt_timeout_s=smelt_timeout_s,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": AUTO_TEMP_FURNACE[0], "y": AUTO_TEMP_FURNACE[1], "z": AUTO_TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(f"auto-site temporary furnace smelt failed: {payload} block_after={block_after.data} inventory={after_inventory}")
    if result.metrics.get("temporary_furnace_site") != list(AUTO_TEMP_FURNACE):
        raise AssertionError(f"auto-site temporary furnace chose wrong site: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(f"auto-site temporary furnace was not reclaimed: block_after={block_after.data} result={payload}")
    if not same_item(after_inventory[3].item, "minecraft:iron_ingot") or after_inventory[3].count != output_count:
        raise AssertionError(f"auto-site temporary furnace did not collect output: slot3={after_inventory[3]} result={payload}")
    smelt = result.metrics.get("smelt") or {}
    executed = (smelt.get("metrics") or {}).get("executed") or []
    input_move = (executed[0].get("result") or {}) if executed else {}
    output_move = (executed[-1].get("result") or {}) if executed else {}
    if ((input_move.get("metrics") or {}).get("count") != input_count) or (
        (output_move.get("metrics") or {}).get("count") != output_count
    ):
        raise AssertionError(
            f"auto-site temporary furnace did not move exact counts: input_count={input_count} output_count={output_count} result={payload}"
        )
    return {
        "reason": result.reason,
        "site": result.metrics.get("temporary_furnace_site"),
        "input_moved": (input_move.get("metrics") or {}).get("count"),
        "output_moved": (output_move.get("metrics") or {}).get("count"),
        "block_after": block_after.data,
        "output_slot": {"item": after_inventory[3].item, "count": after_inventory[3].count},
    }


def run_smelt_temporary_furnace_auto_site_partial_timeout_path(
    rcon: RconClient,
    body: ScarpetBody,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, "minecraft:furnace", 1)
    set_inventory_slot(rcon, 1, "minecraft:raw_iron", 2)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1]} {AUTO_TEMP_FURNACE[2]} air")
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1] - 1} {AUTO_TEMP_FURNACE[2]} stone")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp_auto", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_nearby_temporary_furnace(
        input_item="minecraft:raw_iron",
        input_count=2,
        fuel_item="minecraft:coal",
        output_item="minecraft:iron_ingot",
        output_count=2,
        output_slot=3,
        radius=1,
        poll_interval_s=0.5,
        smelt_timeout_s=12.5,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": AUTO_TEMP_FURNACE[0], "y": AUTO_TEMP_FURNACE[1], "z": AUTO_TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_smelt_failed:smelt_partial_timeout":
        raise AssertionError(f"auto-site temporary furnace partial timeout returned wrong truth: {payload}")
    if result.metrics.get("temporary_furnace_site") != list(AUTO_TEMP_FURNACE):
        raise AssertionError(f"auto-site temporary furnace partial timeout chose wrong site: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(
            f"auto-site temporary furnace partial timeout did not reclaim furnace: block_after={block_after.data} result={payload}"
        )
    if not any(same_item(slot.item, "minecraft:iron_ingot") and slot.count >= 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"auto-site temporary furnace partial timeout lost produced output: inventory={after_inventory} result={payload}"
        )
    smelt = result.metrics.get("smelt") or {}
    partial = (smelt.get("metrics") or {}).get("partial_output") or {}
    partial_result = partial.get("result") or {}
    partial_metrics = partial_result.get("metrics") or {}
    if partial_result.get("reason") != "completed" or int(partial_metrics.get("count") or 0) < 1:
        raise AssertionError(f"auto-site temporary furnace partial timeout did not collect partial output: {payload}")
    reclaim_steps = (smelt.get("metrics") or {}).get("reclaim") or []
    if not reclaim_steps or reclaim_steps[0].get("furnace_slot") != "input":
        raise AssertionError(f"auto-site temporary furnace partial timeout did not expose input reclaim: {payload}")
    return {
        "reason": result.reason,
        "site": result.metrics.get("temporary_furnace_site"),
        "partial_count": partial_metrics.get("count"),
        "output_slot": {"item": after_inventory[3].item, "count": after_inventory[3].count},
        "input_reclaim": reclaim_steps[0],
    }


def run_smelt_temporary_furnace_auto_site_smelt_timeout_path(
    rcon: RconClient,
    body: ScarpetBody,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, "minecraft:furnace", 1)
    set_inventory_slot(rcon, 1, "minecraft:cobblestone", 1)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1]} {AUTO_TEMP_FURNACE[2]} air")
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1] - 1} {AUTO_TEMP_FURNACE[2]} stone")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp_auto", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_nearby_temporary_furnace(
        input_item="minecraft:cobblestone",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=3,
        radius=1,
        poll_interval_s=0.2,
        smelt_timeout_s=1.0,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": AUTO_TEMP_FURNACE[0], "y": AUTO_TEMP_FURNACE[1], "z": AUTO_TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_smelt_failed:smelt_timeout":
        raise AssertionError(f"auto-site temporary furnace smelt-timeout returned wrong truth: {payload}")
    if result.metrics.get("temporary_furnace_site") != list(AUTO_TEMP_FURNACE):
        raise AssertionError(f"auto-site temporary furnace smelt-timeout chose wrong site: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(
            f"auto-site temporary furnace smelt-timeout did not reclaim furnace: block_after={block_after.data} result={payload}"
        )
    if not any(same_item(slot.item, "minecraft:cobblestone") and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(f"auto-site temporary furnace smelt-timeout lost input: inventory={after_inventory} result={payload}")
    if any(same_item(slot.item, "minecraft:iron_ingot") and slot.count > 0 for slot in after_inventory.values()):
        raise AssertionError(
            f"auto-site temporary furnace smelt-timeout produced unexpected output: inventory={after_inventory} result={payload}"
        )
    smelt = result.metrics.get("smelt") or {}
    reclaim_steps = (smelt.get("metrics") or {}).get("reclaim") or []
    if not reclaim_steps or reclaim_steps[0].get("furnace_slot") != "input":
        raise AssertionError(f"auto-site temporary furnace smelt-timeout did not expose input reclaim: {payload}")
    if len(reclaim_steps) < 2 or reclaim_steps[1].get("reason") != "already_empty":
        raise AssertionError(f"auto-site temporary furnace smelt-timeout did not expose consumed fuel truth: {payload}")
    return {
        "reason": result.reason,
        "site": result.metrics.get("temporary_furnace_site"),
        "block_after": block_after.data,
        "input_reclaim": reclaim_steps[0],
        "fuel_reclaim_reason": reclaim_steps[1].get("reason"),
    }


def run_smelt_temporary_special_furnace_auto_site_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    furnace_item: str,
    input_item: str,
    output_item: str,
    input_count: int = 1,
    output_count: int = 1,
    smelt_timeout_s: float = 18.0,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, furnace_item, 1)
    set_inventory_slot(rcon, 1, input_item, input_count)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1]} {AUTO_TEMP_FURNACE[2]} air")
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1] - 1} {AUTO_TEMP_FURNACE[2]} stone")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp_auto", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_nearby_temporary_furnace(
        input_item=input_item,
        input_count=input_count,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item=output_item,
        output_count=output_count,
        output_slot=3,
        furnace_item=furnace_item,
        radius=1,
        poll_interval_s=0.5,
        smelt_timeout_s=smelt_timeout_s,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": AUTO_TEMP_FURNACE[0], "y": AUTO_TEMP_FURNACE[1], "z": AUTO_TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if not result.success or result.reason != "completed":
        raise AssertionError(
            f"auto-site temporary special furnace smelt failed: furnace_item={furnace_item} payload={payload} block_after={block_after.data} inventory={after_inventory}"
        )
    if result.metrics.get("temporary_furnace_site") != list(AUTO_TEMP_FURNACE):
        raise AssertionError(f"auto-site temporary special furnace chose wrong site: furnace_item={furnace_item} result={payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(
            f"auto-site temporary special furnace was not reclaimed: furnace_item={furnace_item} block_after={block_after.data} result={payload}"
        )
    if not same_item(after_inventory[3].item, output_item) or after_inventory[3].count != output_count:
        raise AssertionError(
            f"auto-site temporary special furnace did not collect output: furnace_item={furnace_item} slot3={after_inventory[3]} result={payload}"
        )
    place = ((result.metrics.get("place") or {}).get("metrics") or {}).get("block_type")
    if place not in {furnace_item, f"minecraft:{furnace_item}"}:
        raise AssertionError(
            f"auto-site temporary special furnace placed wrong block type: furnace_item={furnace_item} placed_type={place} result={payload}"
        )
    smelt = result.metrics.get("smelt") or {}
    executed = (smelt.get("metrics") or {}).get("executed") or []
    input_move = (executed[0].get("result") or {}) if executed else {}
    output_move = (executed[-1].get("result") or {}) if executed else {}
    if ((input_move.get("metrics") or {}).get("count") != input_count) or (
        (output_move.get("metrics") or {}).get("count") != output_count
    ):
        raise AssertionError(
            f"auto-site temporary special furnace did not move exact counts: furnace_item={furnace_item} input_count={input_count} output_count={output_count} result={payload}"
        )
    return {
        "reason": result.reason,
        "site": result.metrics.get("temporary_furnace_site"),
        "placed_type": place,
        "input_moved": (input_move.get("metrics") or {}).get("count"),
        "output_moved": (output_move.get("metrics") or {}).get("count"),
        "block_after": block_after.data,
        "output_slot": {"item": after_inventory[3].item, "count": after_inventory[3].count},
    }


def run_smelt_temporary_special_furnace_auto_site_partial_timeout_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    furnace_item: str,
    input_item: str,
    output_item: str,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, furnace_item, 1)
    set_inventory_slot(rcon, 1, input_item, 2)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1]} {AUTO_TEMP_FURNACE[2]} air")
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1] - 1} {AUTO_TEMP_FURNACE[2]} stone")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp_auto", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_nearby_temporary_furnace(
        input_item=input_item,
        input_count=2,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item=output_item,
        output_count=2,
        output_slot=3,
        furnace_item=furnace_item,
        radius=1,
        poll_interval_s=0.5,
        smelt_timeout_s=6.5,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": AUTO_TEMP_FURNACE[0], "y": AUTO_TEMP_FURNACE[1], "z": AUTO_TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_smelt_failed:smelt_partial_timeout":
        raise AssertionError(f"auto-site temporary special furnace partial timeout returned wrong truth: {payload}")
    if result.metrics.get("temporary_furnace_site") != list(AUTO_TEMP_FURNACE):
        raise AssertionError(f"auto-site temporary special furnace partial timeout chose wrong site: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(
            f"auto-site temporary special furnace partial timeout did not reclaim furnace: block_after={block_after.data} result={payload}"
        )
    if not any(same_item(slot.item, output_item) and slot.count >= 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"auto-site temporary special furnace partial timeout lost produced output: furnace_item={furnace_item} inventory={after_inventory} result={payload}"
        )
    smelt = result.metrics.get("smelt") or {}
    partial = (smelt.get("metrics") or {}).get("partial_output") or {}
    partial_result = partial.get("result") or {}
    partial_metrics = partial_result.get("metrics") or {}
    if partial_result.get("reason") != "completed" or int(partial_metrics.get("count") or 0) < 1:
        raise AssertionError(f"auto-site temporary special furnace partial timeout did not collect partial output: {payload}")
    reclaim_steps = (smelt.get("metrics") or {}).get("reclaim") or []
    if not reclaim_steps or reclaim_steps[0].get("furnace_slot") != "input":
        raise AssertionError(f"auto-site temporary special furnace partial timeout did not expose input reclaim: {payload}")
    return {
        "reason": result.reason,
        "site": result.metrics.get("temporary_furnace_site"),
        "partial_count": partial_metrics.get("count"),
        "output_slot": {"item": after_inventory[3].item, "count": after_inventory[3].count},
        "input_reclaim": reclaim_steps[0],
    }


def run_smelt_temporary_special_furnace_auto_site_wrong_recipe_timeout_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    furnace_item: str,
    input_item: str,
    output_item: str,
) -> dict[str, object]:
    reset_flat_world(rcon)
    set_inventory_slot(rcon, 0, furnace_item, 1)
    set_inventory_slot(rcon, 1, input_item, 1)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1]} {AUTO_TEMP_FURNACE[2]} air")
    command(rcon, f"setblock {AUTO_TEMP_FURNACE[0]} {AUTO_TEMP_FURNACE[1] - 1} {AUTO_TEMP_FURNACE[2]} stone")
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp_auto", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    result = runtime.smelt_with_nearby_temporary_furnace(
        input_item=input_item,
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item=output_item,
        output_count=1,
        output_slot=3,
        furnace_item=furnace_item,
        radius=1,
        poll_interval_s=0.2,
        smelt_timeout_s=1.0,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    payload = result.to_payload()
    block_after = body.perceive("blockAt", {"x": AUTO_TEMP_FURNACE[0], "y": AUTO_TEMP_FURNACE[1], "z": AUTO_TEMP_FURNACE[2]})
    after_inventory = inventory_by_slot(body)
    if result.success or result.reason != "temporary_furnace_smelt_failed:smelt_timeout":
        raise AssertionError(f"auto-site temporary special furnace wrong-recipe timeout returned wrong truth: {payload}")
    if result.metrics.get("temporary_furnace_site") != list(AUTO_TEMP_FURNACE):
        raise AssertionError(f"auto-site temporary special furnace wrong-recipe timeout chose wrong site: {payload}")
    if block_after.data.get("state") != "CLEAR":
        raise AssertionError(
            f"auto-site temporary special furnace wrong-recipe timeout did not reclaim furnace: block_after={block_after.data} result={payload}"
        )
    if not any(same_item(slot.item, input_item) and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"auto-site temporary special furnace wrong-recipe timeout lost input: furnace_item={furnace_item} inventory={after_inventory} result={payload}"
        )
    if not any(same_item(slot.item, "minecraft:coal") and slot.count == 1 for slot in after_inventory.values()):
        raise AssertionError(
            f"auto-site temporary special furnace wrong-recipe timeout lost unburned fuel: furnace_item={furnace_item} inventory={after_inventory} result={payload}"
        )
    if any(same_item(slot.item, output_item) and slot.count > 0 for slot in after_inventory.values()):
        raise AssertionError(
            f"auto-site temporary special furnace wrong-recipe timeout produced unexpected output: furnace_item={furnace_item} inventory={after_inventory} result={payload}"
        )
    smelt = result.metrics.get("smelt") or {}
    reclaim_steps = (smelt.get("metrics") or {}).get("reclaim") or []
    if not reclaim_steps or reclaim_steps[0].get("furnace_slot") != "input":
        raise AssertionError(f"auto-site temporary special furnace wrong-recipe timeout did not expose input reclaim: {payload}")
    fuel_reclaim = reclaim_steps[1].get("result") if len(reclaim_steps) > 1 else None
    if not fuel_reclaim or fuel_reclaim.get("reason") != "completed":
        raise AssertionError(f"auto-site temporary special furnace wrong-recipe timeout did not reclaim unburned fuel: {payload}")
    return {
        "reason": result.reason,
        "site": result.metrics.get("temporary_furnace_site"),
        "input_reclaim": reclaim_steps[0],
        "fuel_reclaim_reason": fuel_reclaim.get("reason"),
        "block_after": block_after.data,
    }


def run_smelt_temporary_furnace_no_site_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_flat_world(rcon)
    for x, z in ((0, -1), (-1, 0), (1, 0), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
        command(rcon, f"setblock {x} 59 {z} stone")
    set_inventory_slot(rcon, 0, "minecraft:furnace", 1)
    set_inventory_slot(rcon, 1, "minecraft:raw_iron", 1)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp_auto", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    before = body.get_state()
    result = runtime.smelt_with_nearby_temporary_furnace(
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=3,
        radius=1,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    after = body.get_state()
    after_inventory = inventory_by_slot(body)
    payload = result.to_payload()
    if result.success or result.reason != "temporary_furnace_no_supported_site":
        raise AssertionError(f"auto-site no-site inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"auto-site no-site inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    for slot_index, expected in ((0, "minecraft:furnace"), (1, "minecraft:raw_iron"), (2, "minecraft:coal")):
        slot = after_inventory[slot_index]
        if not same_item(slot.item, expected) or slot.count != 1:
            raise AssertionError(f"auto-site no-site inverse lost slot {slot_index}: inventory={after_inventory} result={payload}")
    return {"reason": result.reason, "before": before.pos, "after": after.pos}


def run_smelt_temporary_special_furnace_no_site_path(
    rcon: RconClient,
    body: ScarpetBody,
    *,
    furnace_item: str,
    input_item: str,
    output_item: str,
) -> dict[str, object]:
    reset_flat_world(rcon)
    for x, z in ((0, -1), (-1, 0), (1, 0), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
        command(rcon, f"setblock {x} 59 {z} stone")
    set_inventory_slot(rcon, 0, furnace_item, 1)
    set_inventory_slot(rcon, 1, input_item, 1)
    set_inventory_slot(rcon, 2, "minecraft:coal", 1)
    set_inventory_slot(rcon, 3, None)
    command(rcon, "script in minebot run minebot_reset()")

    policy = GovernancePolicy(natural_regions=[Region("furnace_temp_auto", (-2, 0, -3), (12, 100, 3))])
    work = BlockWork(body, policy)
    runtime = FurnaceTransactions(body, governance=policy, work=work)
    before = body.get_state()
    result = runtime.smelt_with_nearby_temporary_furnace(
        input_item=input_item,
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item=output_item,
        output_count=1,
        output_slot=3,
        furnace_item=furnace_item,
        radius=1,
        poll_interval_s=0.5,
        smelt_timeout_s=4.0,
        transfer_timeout_s=6.0,
        place_timeout_s=8.0,
        reclaim_timeout_s=8.0,
    )
    after = body.get_state()
    after_inventory = inventory_by_slot(body)
    payload = result.to_payload()
    if result.success or result.reason != "temporary_furnace_no_supported_site":
        raise AssertionError(
            f"auto-site temporary special furnace no-site inverse returned wrong truth: furnace_item={furnace_item} payload={payload}"
        )
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(
            f"auto-site temporary special furnace no-site inverse moved the body: furnace_item={furnace_item} before={before.pos} after={after.pos} result={payload}"
        )
    for slot_index, expected in ((0, furnace_item), (1, input_item), (2, "minecraft:coal")):
        slot = after_inventory[slot_index]
        if not same_item(slot.item, expected) or slot.count != 1:
            raise AssertionError(
                f"auto-site temporary special furnace no-site inverse lost slot {slot_index}: furnace_item={furnace_item} inventory={after_inventory} result={payload}"
            )
    return {"reason": result.reason, "before": before.pos, "after": after.pos}


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
        cases = {
            "transfer_output": lambda: run_transfer_output_path(rcon, body),
            "smelt_once": lambda: run_smelt_once_path(rcon, body),
            "smelt_two_items": lambda: run_smelt_two_items_path(rcon, body),
            "smelt_blast_furnace": lambda: run_smelt_blast_furnace_path(rcon, body),
            "smelt_smoker": lambda: run_smelt_smoker_path(rcon, body),
            "smelt_smoker_wrong_recipe_timeout": lambda: run_smelt_wrong_recipe_timeout_path(
                rcon,
                body,
                furnace_type="smoker",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
            ),
            "smelt_blast_furnace_wrong_recipe_timeout": lambda: run_smelt_wrong_recipe_timeout_path(
                rcon,
                body,
                furnace_type="blast_furnace",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
            ),
            "smelt_not_empty_input": lambda: run_smelt_furnace_not_empty_path(rcon, body, "input"),
            "smelt_not_empty_fuel": lambda: run_smelt_furnace_not_empty_path(rcon, body, "fuel"),
            "smelt_not_empty_output": lambda: run_smelt_furnace_not_empty_path(rcon, body, "output"),
            "smelt_timeout_reclaim": lambda: run_smelt_timeout_reclaim_path(rcon, body),
            "smelt_partial_timeout": lambda: run_smelt_partial_timeout_path(rcon, body),
            "smelt_auto_fuel": lambda: run_smelt_auto_fuel_path(rcon, body),
            "smelt_auto_fuel_insufficient": lambda: run_smelt_auto_fuel_insufficient_path(rcon, body),
            "smelt_auto_stick_fuel": lambda: run_smelt_auto_stick_fuel_path(rcon, body),
            "smelt_input_missing": lambda: run_smelt_input_missing_path(rcon, body),
            "smelt_output_no_space": lambda: run_smelt_output_no_space_path(rcon, body),
            "smelt_unknown_auto_fuel": lambda: run_smelt_unknown_auto_fuel_path(rcon, body),
            "smelt_governance_unknown": lambda: run_smelt_governance_unknown_path(rcon, body),
            "smelt_governance_protected": lambda: run_smelt_governance_protected_path(rcon, body),
            "smelt_nearest": lambda: run_smelt_nearest_path(rcon, body),
            "smelt_nearest_not_found": lambda: run_smelt_nearest_not_found_path(rcon, body),
            "smelt_temporary_furnace": lambda: run_smelt_temporary_furnace_path(rcon, body),
            "smelt_temporary_furnace_two_items": lambda: run_smelt_temporary_furnace_path(
                rcon,
                body,
                input_count=2,
                output_count=2,
                smelt_timeout_s=26.0,
            ),
            "smelt_temporary_smoker": lambda: run_smelt_temporary_special_furnace_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
            ),
            "smelt_temporary_smoker_two_items": lambda: run_smelt_temporary_special_furnace_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
                input_count=2,
                output_count=2,
            ),
            "smelt_temporary_blast_furnace": lambda: run_smelt_temporary_special_furnace_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
            ),
            "smelt_temporary_blast_furnace_two_items": lambda: run_smelt_temporary_special_furnace_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
                input_count=2,
                output_count=2,
            ),
            "smelt_temporary_furnace_smelt_timeout": lambda: run_smelt_temporary_furnace_smelt_timeout_path(rcon, body),
            "smelt_temporary_furnace_partial_timeout": lambda: run_smelt_temporary_furnace_partial_timeout_path(rcon, body),
            "smelt_temporary_smoker_partial_timeout": lambda: run_smelt_temporary_special_furnace_partial_timeout_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
            ),
            "smelt_temporary_blast_furnace_partial_timeout": lambda: run_smelt_temporary_special_furnace_partial_timeout_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
            ),
            "smelt_temporary_smoker_wrong_recipe_timeout": lambda: run_smelt_temporary_special_furnace_wrong_recipe_timeout_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
            ),
            "smelt_temporary_blast_furnace_wrong_recipe_timeout": lambda: run_smelt_temporary_special_furnace_wrong_recipe_timeout_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
            ),
            "smelt_temporary_furnace_occupied": lambda: run_smelt_temporary_furnace_occupied_path(rcon, body),
            "smelt_temporary_furnace_auto_site": lambda: run_smelt_temporary_furnace_auto_site_path(rcon, body),
            "smelt_temporary_furnace_auto_site_two_items": lambda: run_smelt_temporary_furnace_auto_site_path(
                rcon,
                body,
                input_count=2,
                output_count=2,
                smelt_timeout_s=26.0,
            ),
            "smelt_temporary_furnace_auto_site_partial_timeout": lambda: run_smelt_temporary_furnace_auto_site_partial_timeout_path(
                rcon, body
            ),
            "smelt_temporary_furnace_auto_site_smelt_timeout": lambda: run_smelt_temporary_furnace_auto_site_smelt_timeout_path(
                rcon, body
            ),
            "smelt_temporary_smoker_auto_site": lambda: run_smelt_temporary_special_furnace_auto_site_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
            ),
            "smelt_temporary_smoker_auto_site_two_items": lambda: run_smelt_temporary_special_furnace_auto_site_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
                input_count=2,
                output_count=2,
            ),
            "smelt_temporary_blast_furnace_auto_site": lambda: run_smelt_temporary_special_furnace_auto_site_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
            ),
            "smelt_temporary_blast_furnace_auto_site_two_items": lambda: run_smelt_temporary_special_furnace_auto_site_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
                input_count=2,
                output_count=2,
            ),
            "smelt_temporary_furnace_no_site": lambda: run_smelt_temporary_furnace_no_site_path(rcon, body),
            "smelt_temporary_smoker_auto_site_partial_timeout": lambda: run_smelt_temporary_special_furnace_auto_site_partial_timeout_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
            ),
            "smelt_temporary_blast_furnace_auto_site_partial_timeout": lambda: run_smelt_temporary_special_furnace_auto_site_partial_timeout_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
            ),
            "smelt_temporary_smoker_auto_site_wrong_recipe_timeout": lambda: run_smelt_temporary_special_furnace_auto_site_wrong_recipe_timeout_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
            ),
            "smelt_temporary_blast_furnace_auto_site_wrong_recipe_timeout": lambda: run_smelt_temporary_special_furnace_auto_site_wrong_recipe_timeout_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
            ),
            "smelt_temporary_smoker_no_site": lambda: run_smelt_temporary_special_furnace_no_site_path(
                rcon,
                body,
                furnace_item="minecraft:smoker",
                input_item="minecraft:porkchop",
                output_item="minecraft:cooked_porkchop",
            ),
            "smelt_temporary_blast_furnace_no_site": lambda: run_smelt_temporary_special_furnace_no_site_path(
                rcon,
                body,
                furnace_item="minecraft:blast_furnace",
                input_item="minecraft:raw_iron",
                output_item="minecraft:iron_ingot",
            ),
        }
        selected_raw = os.environ.get("MINEBOT_FURNACE_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown MINEBOT_FURNACE_CASES entries: {unknown}; valid={list(cases)}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
