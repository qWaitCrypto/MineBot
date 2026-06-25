#!/usr/bin/env python3
"""give_player Body transaction e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import InteractionTransactions
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


GIVER = "E2EGiveBot"
RECEIVER = "E2ERecvBot"
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
        "time set day",
        "kill @a",
        "kill @e[type=!player]",
        f"player {GIVER} kill",
        f"player {RECEIVER} kill",
        "fill -6 58 -6 6 64 6 stone",
        "fill -4 59 -4 4 62 4 air",
        "fill -4 59 -4 4 62 4 air replace water",
        "fill -4 59 -4 4 62 4 air replace flowing_water",
        "fill -4 59 -4 4 62 4 air replace lava",
        "fill -4 59 -4 4 62 4 air replace flowing_lava",
        "fill -4 58 -4 4 58 4 stone",
    ]:
        command(rcon, cmd)


def inventory_count(body: ScarpetBody, item: str) -> int:
    total = 0
    for slot in body.get_inventory():
        if slot.item in {item, f"minecraft:{item}", item.removeprefix("minecraft:")}:
            total += slot.count
    return total


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
        giver = ScarpetBody(GIVER, rcon)
        receiver = ScarpetBody(RECEIVER, rcon)
        spawn_or_fail(giver, (0, 59, 0))
        spawn_or_fail(receiver, (2, 59, 0))
        command(rcon, f"tp {GIVER} 0 59 0 -90 0")
        command(rcon, f"tp {RECEIVER} 2 59 0 90 0")
        command(rcon, f"gamemode survival {GIVER}")
        command(rcon, f"gamemode survival {RECEIVER}")
        command(rcon, f"effect clear {GIVER}")
        command(rcon, f"effect clear {RECEIVER}")
        command(rcon, f"item replace entity {GIVER} hotbar.0 with air")
        command(rcon, f"item replace entity {GIVER} inventory.9 with diamond 2")
        command(rcon, f"clear {RECEIVER}")
        command(rcon, "script in minebot run minebot_reset()")

        runtime = InteractionTransactions(giver)
        before_receiver = inventory_count(receiver, "minecraft:diamond")
        result = runtime.give_player(
            receiver_name=RECEIVER,
            item="minecraft:diamond",
            count=2,
            pickup_timeout_s=6.0,
        )
        after_receiver = inventory_count(receiver, "minecraft:diamond")

        if not result.success or result.reason != "completed":
            raise AssertionError(f"give_player failed: {result}")
        receipt = dict((result.metrics or {}).get("pickup_receipt") or {})
        if receipt.get("player") != RECEIVER:
            raise AssertionError(f"wrong pickup receiver: {receipt}")
        if int(receipt.get("count") or 0) <= 0:
            raise AssertionError(f"pickup receipt missing count: {receipt}")
        if after_receiver - before_receiver < 1:
            raise AssertionError(
                f"receiver inventory did not increase after handoff: before={before_receiver} after={after_receiver}"
            )

        print(
            {
                "reason": result.reason,
                "receiver": RECEIVER,
                "pickup_receipt": receipt,
                "receiver_inventory_before": before_receiver,
                "receiver_inventory_after": after_receiver,
            }
        )


if __name__ == "__main__":
    main()
