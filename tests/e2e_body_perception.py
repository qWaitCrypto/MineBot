#!/usr/bin/env python3
"""Perception e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EPerceptionBot"
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
        "difficulty normal",
        "time set day",
        "weather clear",
        "kill @e[type=!player]",
        "fill -2 59 -2 2 63 2 air",
        "fill -2 58 -2 2 58 2 stone",
        "setblock 1 59 0 oak_log",
        "setblock 0 59 1 dandelion",
        "summon husk 0 59 2 {NoAI:1b,Health:20f,Tags:[\"minebot_perception_target\"]}",
    ]:
        command(rcon, cmd)


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
        command(rcon, f"tp {BOT} 0 59 0 -90 0")
        command(rcon, f"item replace entity {BOT} hotbar.0 with stone 3")
        command(rcon, "script in minebot run minebot_reset()")

        block = body.perceive("blockAt", {"x": 1, "y": 59, "z": 0})
        if not block.ok or not block.complete:
            raise AssertionError(f"blockAt failed: {block}")
        if block.data.get("type") not in {"oak_log", "minecraft:oak_log"}:
            raise AssertionError(f"blockAt returned wrong type: {block.data}")
        if block.data.get("state") != "SOLID":
            raise AssertionError(f"blockAt returned wrong state: {block.data}")

        nearby = body.perceive("nearbyBlocks", {"radius": 1, "limit": 32})
        if not nearby.ok or not nearby.complete:
            raise AssertionError(f"nearbyBlocks failed: {nearby}")
        blocks = nearby.data.get("blocks") or []
        if not any(item.get("type") in {"oak_log", "minecraft:oak_log"} for item in blocks):
            raise AssertionError(f"nearbyBlocks did not include test oak_log: {nearby.data}")

        found = body.perceive("findBlocks", {"type": "oak_log", "radius": 4, "limit": 8})
        if not found.ok or not found.complete:
            raise AssertionError(f"findBlocks failed: {found}")
        found_blocks = found.data.get("blocks") or []
        if not any(item.get("x") == 1 and item.get("y") == 59 and item.get("z") == 0 for item in found_blocks):
            raise AssertionError(f"findBlocks did not include test oak_log position: {found.data}")

        found_many = body.perceive(
            "findBlocks",
            {"types": ["oak_log", "dandelion"], "radius": 4, "limit": 8},
        )
        if not found_many.ok or not found_many.complete:
            raise AssertionError(f"multi-type findBlocks failed: {found_many}")
        found_types = {
            str(item.get("type") or "").removeprefix("minecraft:")
            for item in found_many.data.get("blocks") or []
        }
        if not {"oak_log", "dandelion"}.issubset(found_types):
            raise AssertionError(f"multi-type findBlocks missed targets: {found_many.data}")

        entities = body.perceive("nearbyEntities", {"radius": 8, "limit": 16})
        if not entities.ok or not entities.complete:
            raise AssertionError(f"nearbyEntities failed: {entities}")
        entity_items = entities.data.get("entities") or []
        if not any(item.get("type") in {"husk", "minecraft:husk"} for item in entity_items):
            raise AssertionError(f"nearbyEntities did not include test husk: {entities.data}")

        inventory = body.perceive("inventory", {"start": 0, "limit": 9})
        if not inventory.ok:
            raise AssertionError(f"inventory failed: {inventory}")
        slots = inventory.data.get("slots") or []
        if not any(item.get("slot") == 0 and item.get("item") in {"stone", "minecraft:stone"} for item in slots):
            raise AssertionError(f"inventory did not include test hotbar stone: {inventory.data}")

        print(
            {
                "blockAt": block.data,
                "nearbyCount": len(blocks),
                "findCount": len(found_blocks),
                "entityCount": len(entity_items),
                "inventorySlots": len(slots),
            }
        )


if __name__ == "__main__":
    main()
