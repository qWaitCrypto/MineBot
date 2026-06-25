#!/usr/bin/env python3
"""Probe give_player receiver stand-point perception on the local test server."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient, ScarpetBody
from minebot.game.rcon import RconConfig
from tests.e2e_body_give_player import GIVER, RECEIVER, command, setup_world
from tests.e2e_support import spawn_or_fail


def main() -> None:
    rcon = RconClient(RconConfig())
    rcon.connect()
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
        command(rcon, "script in minebot run minebot_reset()")
        time.sleep(0.2)

        state = giver.get_state()
        entities = giver.perceive("nearbyEntities", {"radius": 12, "limit": 16})
        print({"giver_pos": state.pos, "entities": entities.data.get("entities")})

        target = (2, 59, 0)
        for pos in ((3, 59, 0), (1, 59, 0), (2, 59, 1), (2, 59, -1)):
            stand = giver.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
            head = giver.perceive("blockAt", {"x": pos[0], "y": pos[1] + 1, "z": pos[2]})
            below = giver.perceive("blockAt", {"x": pos[0], "y": pos[1] - 1, "z": pos[2]})
            print({"target": target, "stand": pos, "feet": stand.data, "head": head.data, "below": below.data})


if __name__ == "__main__":
    main()
