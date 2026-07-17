#!/usr/bin/env python3
"""Farm interaction Body transactions e2e against the local Carpet server."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, InteractionTransactions, NavigationTransactions
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EFarmBot"
FARMLAND = (8, 59, 0)
CROP = (8, 60, 0)
MATURE_CROP = (10, 60, 0)
IMMATURE_FARMLAND = (12, 59, 0)
IMMATURE_CROP = (12, 60, 0)
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
        "gamerule randomTickSpeed 0",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "fill -2 59 -3 16 66 3 air",
        "fill -2 58 -3 16 58 3 stone",
    ]:
        command(rcon, cmd)


def reset_world(rcon: RconClient) -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        f"player {BOT} stop",
        "kill @e[type=!player]",
        "fill -2 59 -3 16 66 3 air",
        "fill -2 58 -3 16 58 3 stone",
        f"clear {BOT}",
        f"effect clear {BOT}",
        f"tp {BOT} 0 59 0 -90 0",
        f"gamemode survival {BOT}",
    ]:
        command(rcon, cmd)


def make_runtime(body: ScarpetBody) -> InteractionTransactions:
    policy = GovernancePolicy(natural_regions=[Region("farm_nav", (-2, 0, -3), (16, 100, 3))])
    navigator = NavigationTransactions.server_side(body, policy)
    work = BlockWork(body, policy, navigator=navigator)
    return InteractionTransactions(body, navigator=navigator, work=work, governance=policy)


def block_fact(body: ScarpetBody, pos: tuple[int, int, int]) -> dict[str, object]:
    block = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    if not block.ok or not block.complete:
        raise AssertionError(f"blockAt failed for {pos}: {block}")
    return dict(block.data)


def inventory_count(body: ScarpetBody, item: str) -> int:
    wanted = item.removeprefix("minecraft:")
    total = 0
    for slot in body.get_inventory():
        actual = (slot.item or "").removeprefix("minecraft:")
        if actual == wanted:
            total += slot.count
    return total


def set_hotbar_slot(rcon: RconClient, slot: int, item_spec: str) -> None:
    command(rcon, f"item replace entity {BOT} hotbar.{slot} with {item_spec}")


def run_till_sow_happy(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon)
    runtime = make_runtime(body)
    command(rcon, f"setblock {FARMLAND[0]} {FARMLAND[1]} {FARMLAND[2]} dirt")
    set_hotbar_slot(rcon, 0, "diamond_hoe 1")
    set_hotbar_slot(rcon, 1, "wheat_seeds 3")

    tilled = runtime.till_farmland(
        hoe_item="minecraft:diamond_hoe",
        pos=FARMLAND,
        approach_timeout_s=18.0,
        use_timeout_s=6.0,
    )
    tilled_payload = tilled.to_payload()
    if not tilled.success or tilled.reason != "tilled":
        raise AssertionError(f"till_farmland failed: {tilled_payload}")
    farmland_after = block_fact(body, FARMLAND)
    if farmland_after.get("type") not in {"farmland", "minecraft:farmland"}:
        raise AssertionError(f"till_farmland did not create farmland: {farmland_after}")

    before_seed_count = inventory_count(body, "wheat_seeds")
    sown = runtime.sow_crop(
        seed_item="minecraft:wheat_seeds",
        farmland_pos=FARMLAND,
        approach_timeout_s=18.0,
        use_timeout_s=6.0,
    )
    sown_payload = sown.to_payload()
    if not sown.success or sown.reason != "sown":
        raise AssertionError(f"sow_crop failed: {sown_payload}")
    crop_after = block_fact(body, CROP)
    if crop_after.get("type") not in {"wheat", "minecraft:wheat"}:
        raise AssertionError(f"sow_crop did not create crop above farmland: {crop_after}")
    after_seed_count = inventory_count(body, "wheat_seeds")
    if after_seed_count != before_seed_count - 1:
        raise AssertionError(
            f"sow_crop did not consume exactly one seed: before={before_seed_count} after={after_seed_count} result={sown_payload}"
        )

    return {
        "till_reason": tilled.reason,
        "sow_reason": sown.reason,
        "farmland_after": farmland_after,
        "crop_after": crop_after,
        "seed_before": before_seed_count,
        "seed_after": after_seed_count,
    }


def run_harvest_and_resow_happy(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon)
    runtime = make_runtime(body)
    command(rcon, f"setblock {MATURE_CROP[0]} {MATURE_CROP[1] - 1} {MATURE_CROP[2]} farmland[moisture=7]")
    command(rcon, f"setblock {MATURE_CROP[0]} {MATURE_CROP[1]} {MATURE_CROP[2]} wheat[age=7]")
    set_hotbar_slot(rcon, 0, "diamond_hoe 1")
    set_hotbar_slot(rcon, 1, "wheat_seeds 1")

    before_wheat = inventory_count(body, "wheat")
    before_seeds = inventory_count(body, "wheat_seeds")
    result = runtime.harvest_and_resow(
        farmland_pos=(MATURE_CROP[0], MATURE_CROP[1] - 1, MATURE_CROP[2]),
        approach_timeout_s=18.0,
        timeout_s=18.0,
        use_timeout_s=6.0,
    )
    payload = result.to_payload()
    if not result.success or result.reason != "harvested_and_resown":
        raise AssertionError(f"harvest_and_resow failed: {payload}")

    farmland_after = block_fact(body, (MATURE_CROP[0], MATURE_CROP[1] - 1, MATURE_CROP[2]))
    crop_after = block_fact(body, MATURE_CROP)
    after_wheat = inventory_count(body, "wheat")
    after_seeds = inventory_count(body, "wheat_seeds")
    if farmland_after.get("type") not in {"farmland", "minecraft:farmland"}:
        raise AssertionError(f"harvest_and_resow lost farmland block: {farmland_after}")
    if crop_after.get("type") not in {"wheat", "minecraft:wheat"}:
        raise AssertionError(f"harvest_and_resow did not re-sow wheat: {crop_after}")
    crop_props = crop_after.get("properties") or {}
    if str(crop_props.get("age") or "") != "0":
        raise AssertionError(f"harvest_and_resow did not re-sow age 0 wheat: {crop_after}")
    if after_wheat < before_wheat + 1:
        raise AssertionError(f"harvest_and_resow did not collect wheat output: before={before_wheat} after={after_wheat}")
    resow_metrics = (((result.metrics or {}).get("resow") or {}).get("metrics") or {})
    if int(resow_metrics.get("seed_delta") or 0) != 1:
        raise AssertionError(
            f"harvest_and_resow did not consume exactly one seed during resow: before={before_seeds} after={after_seeds} result={payload}"
        )

    return {
        "reason": result.reason,
        "harvest_reason": (result.metrics or {}).get("harvest", {}).get("reason"),
        "resow_reason": (result.metrics or {}).get("resow", {}).get("reason"),
        "resow_seed_delta": resow_metrics.get("seed_delta"),
        "farmland_after": farmland_after,
        "crop_after": crop_after,
        "wheat_before": before_wheat,
        "wheat_after": after_wheat,
        "seeds_before": before_seeds,
        "seeds_after": after_seeds,
    }


def run_not_mature_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_world(rcon)
    runtime = make_runtime(body)
    command(rcon, f"setblock {IMMATURE_FARMLAND[0]} {IMMATURE_FARMLAND[1]} {IMMATURE_FARMLAND[2]} farmland[moisture=7]")
    command(rcon, f"setblock {IMMATURE_CROP[0]} {IMMATURE_CROP[1]} {IMMATURE_CROP[2]} wheat[age=3]")
    before = body.get_state()

    result = runtime.harvest_and_resow(
        farmland_pos=IMMATURE_FARMLAND,
        approach_timeout_s=8.0,
        timeout_s=8.0,
        use_timeout_s=4.0,
    )
    after = body.get_state()
    payload = result.to_payload()
    if result.success or result.reason != "harvest_crop_not_mature":
        raise AssertionError(f"harvest_and_resow immature inverse returned wrong truth: {payload}")
    if math.dist(before.pos, after.pos) > 0.75:
        raise AssertionError(f"immature harvest inverse moved the body: before={before.pos} after={after.pos} result={payload}")
    crop_after = block_fact(body, IMMATURE_CROP)
    if str((crop_after.get("properties") or {}).get("age") or "") != "3":
        raise AssertionError(f"immature harvest inverse mutated the crop: {crop_after}")

    return {
        "reason": result.reason,
        "age": result.metrics.get("age") if result.metrics else None,
        "required_age": result.metrics.get("required_age") if result.metrics else None,
        "before": before.pos,
        "after": after.pos,
        "crop_after": crop_after,
    }


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

        print(
            {
                "till_sow": run_till_sow_happy(rcon, body),
                "harvest_resow": run_harvest_and_resow_happy(rcon, body),
                "not_mature": run_not_mature_inverse(rcon, body),
            }
        )


if __name__ == "__main__":
    main()
