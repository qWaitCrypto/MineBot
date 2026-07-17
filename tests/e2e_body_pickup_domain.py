#!/usr/bin/env python3
"""Live gate for planner-owned dropped-item pickup domains."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import NavigationTransactions, PickupConfig, PickupTransactions  # noqa: E402
from minebot.body.inventory_read import read_inventory_counts  # noqa: E402
from minebot.body.world_read import read_block_facts  # noqa: E402
from minebot.game import GovernancePolicy, ScarpetBody  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "PickupDomain"
BASE = (520, 120, 520)


def command(rcon, text: str, *, delay: float = 0.05) -> str:
    result = rcon.command(text)
    if delay:
        time.sleep(delay)
    return result


def setup_world(rcon) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[tuple[int, int, int], ...]]:
    x, y, z = BASE
    command(rcon, f"forceload add {x - 2} {z - 4} {x + 12} {z + 4}")
    command(rcon, f"fill {x - 2} {y - 2} {z - 4} {x + 12} {y + 4} {z + 4} air")
    command(rcon, f"fill {x - 2} {y - 1} {z - 4} {x + 12} {y - 1} {z + 4} stone")
    command(rcon, f"fill {x + 2} {y} {z - 1} {x + 4} {y + 2} {z + 1} stone")
    command(rcon, f"setblock {x + 3} {y} {z} air")
    command(rcon, f"setblock {x + 3} {y + 1} {z} air")
    command(rcon, f"kill @e[type=item,x={x - 4},y={y - 4},z={z - 6},dx=22,dy=10,dz=12]")

    blocked = (x + 3, y, z)
    reachable = (x + 8, y, z)
    command(
        rcon,
        f'summon item {blocked[0] + 0.5} {blocked[1] + 0.2} {blocked[2] + 0.5} '
        '{Tags:["minebot.pickup.blocked"],NoGravity:1b,PickupDelay:0,Item:{id:"minecraft:dirt",count:1}}',
    )
    command(
        rcon,
        f'summon item {reachable[0] + 0.5} {reachable[1] + 0.2} {reachable[2] + 0.5} '
        '{Tags:["minebot.pickup.reachable"],NoGravity:1b,PickupDelay:0,Item:{id:"minecraft:dirt",count:1}}',
    )
    shell = tuple(
        (bx, by, bz)
        for bx in range(x + 2, x + 5)
        for by in range(y, y + 3)
        for bz in range(z - 1, z + 2)
    )
    return blocked, reachable, shell


def block_snapshot(body: ScarpetBody, positions) -> dict[tuple[int, int, int], tuple[str, str]]:
    facts = read_block_facts(body, positions, failure_label="pickup_domain_shell")
    return {
        pos: (str(fact.data.get("type") or "unknown"), str(fact.data.get("state") or "UNKNOWN"))
        for pos, fact in facts.items()
    }


def main() -> None:
    with connect_or_skip() as rcon:
        for text in (
            "script unload minebot",
            "script load minebot global",
            "script in minebot run minebot_reset()",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule doMobSpawning false",
            f"player {BOT} kill",
        ):
            command(rcon, text)

        blocked, reachable, shell = setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, BASE)
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        before_world = block_snapshot(body, shell)

        navigator = NavigationTransactions.server_side(body, GovernancePolicy())
        pickup = PickupTransactions(body, navigator)
        result = pickup.pickup_items(
            expected_items=("dirt",),
            minimum_count=1,
            config=PickupConfig(
                radius=12,
                entity_limit=16,
                max_goals=16,
                max_scan_rounds=2,
                candidate_budget=2,
                max_wall_s=30.0,
                poll_timeout_s=1.0,
                segment_timeout_s=8.0,
                max_segments=4,
            ),
        )

        counts = read_inventory_counts(body)
        if not isinstance(counts, dict):
            raise AssertionError(f"inventory read failed: {counts}")
        after_world = block_snapshot(body, shell)
        plans = list((result.metrics or {}).get("pickup_process", {}).get("plans") or [])
        if not result.success or counts.get("dirt", 0) < 1:
            raise AssertionError(f"pickup domain did not collect reachable item: {result.to_payload()}")
        if not plans or plans[0].get("selected_goal") != list(reachable):
            raise AssertionError(
                f"planner did not select reachable alternative blocked={blocked} reachable={reachable}: {result.to_payload()}"
            )
        if before_world != after_world:
            raise AssertionError("pickup movement mutated the sealed obstacle")

        print(
            "PICKUP DOMAIN PASSED "
            f"selected={plans[0]['selected_goal']} blocked={list(blocked)} inventory_dirt={counts.get('dirt', 0)} "
            f"goal_count={len(plans[0].get('goal_set') or [])} world_unchanged={before_world == after_world}"
        )
        command(rcon, f"player {BOT} kill")
        command(rcon, f"forceload remove {BASE[0] - 2} {BASE[2] - 4} {BASE[0] + 12} {BASE[2] + 4}")


if __name__ == "__main__":
    main()
