#!/usr/bin/env python3
"""container perception e2e against the local Carpet test server."""

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


BOT = "E2EContViewBot"
SKIP_EXIT_CODE = 77
CHEST = (1, 59, 0)


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
        "fill -2 59 -2 2 62 2 air",
        "fill -2 58 -2 2 58 2 stone",
        f"setblock {CHEST[0]} {CHEST[1]} {CHEST[2]} chest",
        f"item replace block {CHEST[0]} {CHEST[1]} {CHEST[2]} container.0 with diamond 3",
        f"item replace block {CHEST[0]} {CHEST[1]} {CHEST[2]} container.1 with cobblestone 8",
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
        command(rcon, f"tp {BOT} 0 59 0")
        command(rcon, "script in minebot run minebot_reset()")

        slots = body.get_container(CHEST, total_slots=27, page_size=2)
        by_slot = {slot.slot: slot for slot in slots}
        if by_slot[0].item not in {"diamond", "minecraft:diamond"} or by_slot[0].count != 3:
            raise AssertionError(f"wrong slot 0 contents: {by_slot[0]}")
        if by_slot[1].item not in {"cobblestone", "minecraft:cobblestone"} or by_slot[1].count != 8:
            raise AssertionError(f"wrong slot 1 contents: {by_slot[1]}")
        if len(slots) != 27:
            raise AssertionError(f"expected full chest page walk, got {len(slots)} slots")

        print(
            {
                "slots": len(slots),
                "slot0": {"item": by_slot[0].item, "count": by_slot[0].count},
                "slot1": {"item": by_slot[1].item, "count": by_slot[1].count},
            }
        )


if __name__ == "__main__":
    main()
