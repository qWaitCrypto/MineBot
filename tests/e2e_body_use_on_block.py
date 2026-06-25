#!/usr/bin/env python3
"""UseTransactions use-item/use-on-target e2e against the local Carpet test server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import UseTransactions
from minebot.body.interaction_support import DirectInteractionNavigator
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EUseOnBot"
FIRE_TARGET = (0, 70, 2)
WATER_TARGET = (2, 70, 2)
RECOVERY_FAIL_TARGET = (0, 64, 2)
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
        "fill -10 70 -4 10 78 10 air",
        "fill -10 70 -4 10 78 10 air replace water",
        "fill -10 70 -4 10 78 10 air replace flowing_water",
        "fill -10 70 -4 10 78 10 air replace lava",
        "fill -10 70 -4 10 78 10 air replace flowing_lava",
        "fill -10 69 -4 10 69 10 stone",
        f"player {BOT} kill",
    ]:
        command(rcon, cmd)


def reset_subject(rcon: RconClient) -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        "kill @e[type=!player]",
        "fill -10 70 -4 10 78 10 air",
        "fill -10 70 -4 10 78 10 air replace water",
        "fill -10 70 -4 10 78 10 air replace flowing_water",
        "fill -10 70 -4 10 78 10 air replace lava",
        "fill -10 70 -4 10 78 10 air replace flowing_lava",
        "fill -10 69 -4 10 69 10 stone",
        f"clear {BOT}",
        f"effect clear {BOT}",
        f"tp {BOT} 0 70 0 -90 0",
        f"gamemode survival {BOT}",
    ]:
        command(rcon, cmd)


def set_hotbar_slot(rcon: RconClient, slot: int, item_spec: str) -> None:
    command(rcon, f"item replace entity {BOT} hotbar.{slot} with {item_spec}")


def inventory_count(body: ScarpetBody, item: str) -> int:
    wanted = item.removeprefix("minecraft:")
    total = 0
    for slot in body.get_inventory():
        actual = (slot.item or "").removeprefix("minecraft:")
        if actual == wanted:
            total += slot.count
    return total


def block_fact(body: ScarpetBody, pos: tuple[int, int, int]) -> dict[str, object]:
    perception = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    if not (perception.ok and perception.complete):
        raise AssertionError(f"blockAt failed for {pos}: {perception}")
    return dict(perception.data)


def run_fire_happy(body: ScarpetBody, runtime: UseTransactions, rcon: RconClient) -> dict[str, object]:
    reset_subject(rcon)
    command(rcon, f"setblock {FIRE_TARGET[0]} {FIRE_TARGET[1] - 1} {FIRE_TARGET[2]} netherrack")
    command(rcon, f"setblock {FIRE_TARGET[0]} {FIRE_TARGET[1]} {FIRE_TARGET[2]} air")
    set_hotbar_slot(rcon, 0, "flint_and_steel 1")
    command(rcon, f"tp {BOT} 1 70 2 180 0")
    look_target = (FIRE_TARGET[0] + 0.5, FIRE_TARGET[1] - 0.2, FIRE_TARGET[2] + 0.5)

    before = block_fact(body, FIRE_TARGET)
    result = runtime.use_on_block(
        pos=FIRE_TARGET,
        item="minecraft:flint_and_steel",
        expected_block_types=("fire",),
        look_target=look_target,
        navigation_arrival_radius=0.25,
        timeout_s=6.0,
    )
    after = block_fact(body, FIRE_TARGET)

    if not result.success or result.reason != "completed":
        raise AssertionError(f"fire happy path failed: {result}")
    if before.get("type") not in {"air", "minecraft:air"}:
        raise AssertionError(f"unexpected fire setup before use: {before}")
    if after.get("type") not in {"fire", "minecraft:fire"}:
        raise AssertionError(f"target block did not change to fire: {after}")
    if ((result.metrics or {}).get("use") or {}).get("metrics", {}).get("method") not in {"physical", "substitute"}:
        raise AssertionError(f"ignite method missing or invalid: {result.metrics}")

    return {"reason": result.reason, "before": before, "after": after, "metrics": result.metrics}


def run_fire_los_recovery(body: ScarpetBody, runtime: UseTransactions, rcon: RconClient) -> dict[str, object]:
    reset_subject(rcon)
    command(rcon, f"setblock {FIRE_TARGET[0]} {FIRE_TARGET[1] - 1} {FIRE_TARGET[2]} netherrack")
    command(rcon, f"setblock {FIRE_TARGET[0]} {FIRE_TARGET[1]} {FIRE_TARGET[2]} air")
    set_hotbar_slot(rcon, 0, "flint_and_steel 1")
    command(rcon, f"tp {BOT} -1 70 2 0 0")
    look_target = (FIRE_TARGET[0] + 0.5, FIRE_TARGET[1] - 0.2, FIRE_TARGET[2] + 0.5)

    result = runtime.use_on_block(
        pos=FIRE_TARGET,
        item="minecraft:flint_and_steel",
        expected_block_types=("fire",),
        look_target=look_target,
        navigation_arrival_radius=0.25,
        timeout_s=6.0,
        line_of_sight_retries=3,
    )
    after = block_fact(body, FIRE_TARGET)
    recovery = (result.metrics or {}).get("line_of_sight_recovery") or {}

    if not result.success or result.reason != "completed":
        raise AssertionError(f"fire LOS recovery failed: {result}")
    if after.get("type") not in {"fire", "minecraft:fire"}:
        raise AssertionError(f"fire LOS recovery did not ignite target: {after}")
    if ((result.metrics or {}).get("use") or {}).get("metrics", {}).get("method") not in {"physical", "substitute"}:
        raise AssertionError(f"ignite method missing or invalid: {result.metrics}")

    return {
        "reason": result.reason,
        "after": after,
        "recovery": recovery,
        "direct_success_without_reposition": recovery.get("repositioned") is not True,
    }


def run_water_bucket_place_collect(body: ScarpetBody, runtime: UseTransactions, rcon: RconClient) -> dict[str, object]:
    reset_subject(rcon)
    command(rcon, f"setblock {WATER_TARGET[0]} {WATER_TARGET[1] - 1} {WATER_TARGET[2]} stone")
    command(rcon, f"setblock {WATER_TARGET[0]} {WATER_TARGET[1]} {WATER_TARGET[2]} air")
    set_hotbar_slot(rcon, 0, "water_bucket 1")
    look_target = (WATER_TARGET[0] + 0.5, WATER_TARGET[1] - 0.2, WATER_TARGET[2] + 0.5)

    placed = runtime.use_on_block(
        pos=WATER_TARGET,
        item="minecraft:water_bucket",
        expected_block_types=("water",),
        look_target=look_target,
        watched_items=("bucket",),
        required_watched_item_deltas={"bucket": 1},
        navigation_arrival_radius=0.25,
        timeout_s=6.0,
    )
    after_place = block_fact(body, WATER_TARGET)
    if not placed.success or placed.reason != "completed":
        raise AssertionError(f"water bucket place failed: {placed}")
    if after_place.get("type") not in {"water", "minecraft:water"}:
        raise AssertionError(f"water bucket place did not create water: {after_place}")
    if inventory_count(body, "bucket") <= 0:
        raise AssertionError(f"water bucket place did not leave bucket remainder: {body.get_inventory()}")

    collected = runtime.use_on_block(
        pos=WATER_TARGET,
        item="minecraft:bucket",
        expected_block_types=("air",),
        look_target=look_target,
        watched_items=("water_bucket",),
        required_watched_item_deltas={"water_bucket": 1},
        navigation_arrival_radius=0.25,
        timeout_s=6.0,
    )
    after_collect = block_fact(body, WATER_TARGET)
    if not collected.success or collected.reason != "completed":
        raise AssertionError(f"water bucket collect failed: {collected}")
    if after_collect.get("type") not in {"air", "minecraft:air"}:
        raise AssertionError(f"water bucket collect did not clear water: {after_collect}")
    if inventory_count(body, "water_bucket") <= 0:
        raise AssertionError(f"water bucket collect did not restore water bucket: {body.get_inventory()}")

    return {
        "place_reason": placed.reason,
        "collect_reason": collected.reason,
        "after_place": after_place,
        "after_collect": after_collect,
        "place_metrics": placed.metrics,
        "collect_metrics": collected.metrics,
    }


def run_entity_bucket(body: ScarpetBody, runtime: UseTransactions, rcon: RconClient) -> dict[str, object]:
    reset_subject(rcon)
    command(rcon, "summon cow 3 70 0 {NoAI:1b}")
    set_hotbar_slot(rcon, 0, "bucket 1")

    result = runtime.use_on_entity(
        item="minecraft:bucket",
        entity_types=("cow",),
        watched_items=("milk_bucket",),
        required_watched_item_deltas={"milk_bucket": 1},
        timeout_s=6.0,
    )
    if not result.success or result.reason != "completed":
        raise AssertionError(f"bucket-on-cow failed: {result}")
    if inventory_count(body, "milk_bucket") <= 0:
        raise AssertionError(f"bucket-on-cow did not create milk bucket: {body.get_inventory()}")

    return {"reason": result.reason, "metrics": result.metrics}


def run_ender_pearl(body: ScarpetBody, runtime: UseTransactions, rcon: RconClient) -> dict[str, object]:
    reset_subject(rcon)
    set_hotbar_slot(rcon, 0, "ender_pearl 2")
    command(rcon, f"tp {BOT} 0 70 0 -90 -10")

    before = body.get_state().pos
    result = runtime.use_item(
        item="minecraft:ender_pearl",
        look_target=(20.0, 72.0, 0.0),
        min_position_delta=5.0,
        use_mode="once",
        timeout_s=8.0,
    )
    after = body.get_state().pos
    moved = math.dist(before, after)

    if not result.success or result.reason != "completed":
        raise AssertionError(f"ender pearl use failed: {result}")
    if moved < 5.0:
        raise AssertionError(f"ender pearl did not move the body enough: before={before} after={after} moved={moved}")
    if inventory_count(body, "ender_pearl") != 1:
        raise AssertionError(f"ender pearl count did not decrement: {body.get_inventory()}")

    return {"reason": result.reason, "before": before, "after": after, "moved": moved, "metrics": result.metrics}


def run_fire_los_recovery_inverse(body: ScarpetBody, runtime: UseTransactions, rcon: RconClient) -> dict[str, object]:
    reset_subject(rcon)
    command(rcon, f"setblock {RECOVERY_FAIL_TARGET[0]} {RECOVERY_FAIL_TARGET[1] - 1} {RECOVERY_FAIL_TARGET[2]} netherrack")
    command(rcon, f"setblock {RECOVERY_FAIL_TARGET[0]} {RECOVERY_FAIL_TARGET[1]} {RECOVERY_FAIL_TARGET[2]} air")
    set_hotbar_slot(rcon, 0, "flint_and_steel 1")
    command(rcon, f"tp {BOT} -1 64 2 0 0")

    before = body.get_state().pos
    result = runtime.use_on_block(
        pos=RECOVERY_FAIL_TARGET,
        item="minecraft:flint_and_steel",
        expected_block_types=("fire",),
        timeout_s=6.0,
        line_of_sight_retries=3,
        navigation_arrival_radius=0.25,
    )
    after = body.get_state().pos
    recovery = (result.metrics or {}).get("line_of_sight_recovery") or {}
    target_after = block_fact(body, RECOVERY_FAIL_TARGET)

    if result.success or result.reason != "targeted_use_no_effect":
        raise AssertionError(f"fire LOS recovery inverse returned wrong truth: {result}")
    if recovery.get("repositioned") is not True:
        raise AssertionError(f"fire LOS recovery inverse did not actually reposition: {result.metrics}")
    if target_after.get("type") not in {"air", "minecraft:air"}:
        raise AssertionError(f"fire LOS recovery inverse unexpectedly changed target: {target_after}")
    if math.dist(before, after) <= 0.75:
        raise AssertionError(f"fire LOS recovery inverse did not move during retry attempt: before={before} after={after} result={result}")

    return {"reason": result.reason, "before": before, "after": after, "recovery": recovery, "target_after": target_after}


def run_entity_not_found_inverse(body: ScarpetBody, runtime: UseTransactions, rcon: RconClient) -> dict[str, object]:
    reset_subject(rcon)
    before = body.get_state().pos
    result = runtime.use_on_entity(item="minecraft:bucket", entity_types=("cow",), search_radius=6, timeout_s=4.0)
    after = body.get_state().pos

    if result.success or result.reason != "use_entity_not_found":
        raise AssertionError(f"use_on_entity not-found inverse returned wrong truth: {result}")
    if math.dist(before, after) > 0.75:
        raise AssertionError(f"use_on_entity not-found inverse moved the body: before={before} after={after} result={result}")

    return {"reason": result.reason, "before": before, "after": after, "metrics": result.metrics}


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
        runtime = UseTransactions(body, navigator=DirectInteractionNavigator(body))
        spawn_or_fail(body, (0, 70, 0))

        print(
            {
                "fire_happy": run_fire_happy(body, runtime, rcon),
                "fire_los_recovery": run_fire_los_recovery(body, runtime, rcon),
                "water_bucket_place_collect": run_water_bucket_place_collect(body, runtime, rcon),
                "entity_bucket": run_entity_bucket(body, runtime, rcon),
                "ender_pearl": run_ender_pearl(body, runtime, rcon),
                "fire_los_recovery_inverse": run_fire_los_recovery_inverse(body, runtime, rcon),
                "entity_not_found": run_entity_not_found_inverse(body, runtime, rcon),
            }
        )


if __name__ == "__main__":
    main()
