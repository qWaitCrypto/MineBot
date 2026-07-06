#!/usr/bin/env python3
"""Harvest-tier e2e for mine_block_collect against the local Carpet test server."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, NavigationTransactions
from minebot.game import ScarpetBody
from minebot.game.governance import GovernancePolicy, Region
from minebot.game.rcon import RconConfig
from tests.e2e_support import connect_or_skip, spawn_or_fail

BOT = "E2EMineTierBot"
REGION = Region("mine-tier", (120, 60, -8), (132, 80, 8))
TARGET = (125, 70, 0)


def command(rcon, command_text: str, delay: float = 0.05) -> str:
    out = rcon.command(command_text)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon) -> None:
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
        "fill 120 70 -8 132 76 8 air",
        "fill 120 69 -8 132 69 8 stone",
    ]:
        command(rcon, cmd)


def reset_case(rcon, body: ScarpetBody, inventory_item: str | None = None) -> None:
    command(rcon, f"tp {BOT} 124 70 0 -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} diamond_ore")
    _clear_inventory(body)
    if inventory_item is not None:
        _set_inventory_slot(body, 9, f"minecraft:{inventory_item}", 1)
    command(rcon, "script in minebot run minebot_reset()")


def block_type(body: ScarpetBody, pos: tuple[int, int, int]) -> str:
    perception = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    if not perception.ok:
        raise AssertionError(f"block perception failed: {perception}")
    return str(perception.data.get("type") or "unknown").removeprefix("minecraft:")


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
    with connect_or_skip(RconConfig()) as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (124, 70, 0))
        policy = GovernancePolicy(natural_regions=[REGION])
        navigator = NavigationTransactions.server_side(body, policy)
        work = BlockWork(body, policy, navigator=navigator)

        reset_case(rcon, body)
        missing = work.mine_block_collect(TARGET, expected_drops=("minecraft:diamond",), timeout_s=10.0)
        if missing.success or missing.reason != "missing_required_tool":
            raise AssertionError(f"empty-hand tier gate failed: {missing.to_payload()}")
        if block_type(body, TARGET) != "diamond_ore":
            raise AssertionError(f"empty-hand tier gate mutated target: {block_type(body, TARGET)}")

        reset_case(rcon, body, "stone_pickaxe")
        weak = work.mine_block_collect(TARGET, expected_drops=("minecraft:diamond",), timeout_s=10.0)
        if weak.success or weak.reason != "missing_required_tool":
            raise AssertionError(f"stone-pickaxe tier gate failed: {weak.to_payload()}")
        if block_type(body, TARGET) != "diamond_ore":
            raise AssertionError(f"stone-pickaxe tier gate mutated target: {block_type(body, TARGET)}")

        reset_case(rcon, body, "iron_pickaxe")
        before = _inventory_counts(body)
        mined = work.mine_block_collect(TARGET, expected_drops=("minecraft:diamond",), pickup_timeout_s=2.0, timeout_s=15.0)
        after = _inventory_counts(body)
        if not mined.success or mined.reason != "collected":
            raise AssertionError(f"iron-pickaxe mine failed: {mined.to_payload()} before={before} after={after}")
        if int(after.get("diamond", 0)) - int(before.get("diamond", 0)) < 1:
            raise AssertionError(f"diamond delta missing: before={before} after={after} result={mined.to_payload()}")
        if block_type(body, TARGET) != "air":
            raise AssertionError(f"iron-pickaxe mine did not clear target: {block_type(body, TARGET)}")
        if (mined.metrics or {}).get("tool_gate", {}).get("selected_item") != "iron_pickaxe":
            raise AssertionError(f"auto-equip metrics missing: {mined.to_payload()}")

        print(
            {
                "missing_reason": missing.reason,
                "weak_reason": weak.reason,
                "mined_reason": mined.reason,
                "diamond_delta": int(after.get("diamond", 0)) - int(before.get("diamond", 0)),
                "selected_item": (mined.metrics or {}).get("tool_gate", {}).get("selected_item"),
            }
        )


if __name__ == "__main__":
    main()
