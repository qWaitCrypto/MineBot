#!/usr/bin/env python3
"""Physical jump e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig


BOT = "E2EJumpBot"
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
        "fill -4 59 -4 4 66 4 air",
        "fill -4 58 -4 4 58 4 stone",
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
        body.spawn((0, 59, 0))
        command(rcon, f"tp {BOT} 0 59 0 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")
        command(rcon, "script in minebot run minebot_reset()")

        start = body.get_state()
        terminal = body.jump(timeout_s=2.0)

        peak_y = start.pos[1]
        samples: list[tuple[float, float, float]] = []
        deadline = time.time() + 0.6
        while time.time() < deadline:
            state = body.get_state()
            samples.append(state.pos)
            peak_y = max(peak_y, state.pos[1])
            time.sleep(0.05)

        if terminal.name != "jumpDone":
            raise AssertionError(f"expected jumpDone, got {terminal}")
        if not terminal.data.get("success", False):
            raise AssertionError(f"jumpDone did not report success: {terminal}")
        if peak_y <= start.pos[1] + 0.2:
            raise AssertionError(
                f"jump did not produce observable height gain: start={start.pos} peak_y={peak_y:.3f} samples={samples}"
            )

        print(
            {
                "start": start.pos,
                "terminal": terminal.data,
                "peak_y": round(peak_y, 3),
                "samples": samples[:4] + (["..."] if len(samples) > 8 else []) + samples[-4:],
            }
        )


if __name__ == "__main__":
    main()
