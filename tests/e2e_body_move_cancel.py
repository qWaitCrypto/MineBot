#!/usr/bin/env python3
"""Delayed movement cancellation e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import Action, RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig


BOT = "E2ECancelBot"
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
        "fill -4 59 -4 18 66 4 air",
        "fill -4 58 -4 18 58 4 stone",
    ]:
        command(rcon, cmd)


def wait_for_event(body: ScarpetBody, name: str, action_id: str, timeout_s: float = 8.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in body.poll_events():
            if event.name == name and event.data.get("action_id") == action_id:
                return event
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {name} for action {action_id}")


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
        command(rcon, f"tp {BOT} 0 59 0 -90 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")
        command(rcon, "script in minebot run minebot_reset()")

        target = (14, 59, 0)
        movement_cancel = {
            "safe_to_cancel": False,
            "unsafe_count": 1,
            "unsafe_steps": [
                {
                    "index": 0,
                    "pos": list(target),
                    "move": "fall",
                    "policy": "land_first",
                }
            ],
        }
        action = Action.create(
            "moveTo",
            {
                "target": list(target),
                "waypoints": [list(target)],
                "arrival_radius": 0.75,
                "timeout_ticks": 500,
                "no_progress_ticks": 120,
                "max_deviation": 8.0,
                "movement_cancel": movement_cancel,
            },
        )
        accepted = body.execute(action)
        if not accepted.ok or not accepted.accepted:
            raise AssertionError(f"moveTo was not accepted: {accepted}")

        time.sleep(0.25)
        interrupt = body.interrupt("test_delayed_cancel")
        if not interrupt.ok or not interrupt.accepted:
            raise AssertionError(f"interrupt was not accepted: {interrupt}")

        delayed = wait_for_event(body, "moveCancelDelayed", action.id, timeout_s=5.0)
        terminal = body.await_action_terminal(action.id, timeout_s=8.0)

        if terminal.name != "moveDone":
            raise AssertionError(f"expected delayed moveDone, got {terminal}")
        if terminal.data.get("stopped_reason") != "interrupted":
            raise AssertionError(f"expected interrupted moveDone, got {terminal}")
        if terminal.seq <= delayed.seq:
            raise AssertionError(f"terminal event did not follow delayed event: {delayed} -> {terminal}")
        if terminal.tick <= delayed.tick:
            raise AssertionError(f"terminal was not delayed by tick time: {delayed} -> {terminal}")

        delayed_cancel = delayed.data.get("movement_cancel") or {}
        terminal_cancel = terminal.data.get("movement_cancel") or {}
        if delayed_cancel.get("unsafe_count") != 1 or terminal_cancel.get("unsafe_count") != 1:
            raise AssertionError(f"movement_cancel facts not preserved: {delayed} -> {terminal}")

        print(
            {
                "delayed": delayed.data,
                "terminal": terminal.data,
                "tick_delay": terminal.tick - delayed.tick,
                "seq_delay": terminal.seq - delayed.seq,
            }
        )


if __name__ == "__main__":
    main()
