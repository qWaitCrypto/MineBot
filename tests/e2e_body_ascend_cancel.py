#!/usr/bin/env python3
"""Live regression gate for a delayed cancel during a blocked ascend."""

from __future__ import annotations

import math
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import NavigationRunConfig, NavigationTransactions
from minebot.game import GovernancePolicy, ScarpetBody
from minebot.game.rcon import RconConfig
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "AscendCancel"
BASE_X = 300
BASE_Y = 120
BASE_Z = 300


def command(rcon, text: str, *, delay: float = 0.05) -> str:
    result = rcon.command(text)
    if delay:
        time.sleep(delay)
    return result


class AscendObstructionBody(ScarpetBody):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.injected = False
        self.injected_target: tuple[int, int, int] | None = None

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs):
        deadline = time.monotonic() + min(timeout_s, 4.0)
        entered_ascend = False
        while not self.injected and time.monotonic() < deadline:
            for event in self.poll_events():
                if event.data.get("action_id") != action_id:
                    continue
                if event.name != "moveKindChanged" or event.data.get("to") != "ascend":
                    continue
                entered_ascend = True
                target = event.data.get("target")
                if not isinstance(target, list) or len(target) != 3:
                    raise AssertionError(f"ascend transition omitted target: {event}")
                self.injected_target = tuple(math.floor(float(value)) for value in target)
            if entered_ascend and self.injected_target is not None:
                state = self.get_state()
                if state.pos[1] > BASE_Y + 0.15:
                    tx, ty, tz = self.injected_target
                    command(self.transport, f"setblock {tx} {ty} {tz} stone", delay=0.0)
                    command(self.transport, f"setblock {tx} {ty + 1} {tz} stone", delay=0.0)
                    self.injected = True
                    break
            if not self.injected:
                time.sleep(0.02)
        if not self.injected:
            raise AssertionError("navigation never entered the expected ascend transition")
        return super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)


def setup_world(rcon) -> None:
    x, y, z = BASE_X, BASE_Y, BASE_Z
    for text in (
        "script resume",
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "difficulty peaceful",
        "time set day",
        "weather clear",
        "kill @e[type=!player]",
        f"fill {x - 10} {y - 10} {z - 10} {x + 30} {y + 10} {z + 10} air",
        f"fill {x - 10} {y - 1} {z - 10} {x + 30} {y - 1} {z + 10} stone",
        f"fill {x + 2} {y} {z - 1} {x + 10} {y} {z + 1} stone",
        f"fill {x - 10} {y} {z - 1} {x + 30} {y + 3} {z - 1} stone",
        f"fill {x - 10} {y} {z + 1} {x + 30} {y + 3} {z + 1} stone",
        "script in minebot run minebot_reset()",
    ):
        command(rcon, text)


def main() -> None:
    with connect_or_skip(RconConfig(timeout_s=15.0, reconnect_attempts=1)) as rcon:
        setup_world(rcon)
        body = AscendObstructionBody(BOT, rcon)
        try:
            body.despawn()
            spawn_or_fail(body, (BASE_X, BASE_Y, BASE_Z))
            command(rcon, f"tp {BOT} {BASE_X + 0.5} {BASE_Y} {BASE_Z + 0.5}")
            command(rcon, f"gamemode survival {BOT}")
            command(rcon, f"effect clear {BOT}")
            command(rcon, f"player {BOT} stop")
            body.poll_events()

            result = NavigationTransactions.server_side(body, GovernancePolicy()).navigate_to(
                (BASE_X + 6, BASE_Y + 1, BASE_Z),
                config=NavigationRunConfig(
                    max_segments=1,
                    segment_timeout_s=6.0,
                    min_partial_progress=1,
                    allow_break=False,
                    allow_place=False,
                    allow_pillar=False,
                    allow_downward=False,
                    allow_open=False,
                    recovery_attempts=0,
                ),
            )
            time.sleep(0.25)
            head = body.event_head("ascend-cancel-e2e")
            events = [
                event
                for event in body.event_log
                if event.name in {"moveCancelDelayed", "moveDone", "navigateDone"}
            ]
            delayed = next((event for event in events if event.name == "moveCancelDelayed"), None)
            move_done = next((event for event in events if event.name == "moveDone"), None)
            navigate_done = next((event for event in events if event.name == "navigateDone"), None)
            on_ground = "1b" in command(rcon, f"data get entity {BOT} OnGround", delay=0.0)

            if not body.injected or body.injected_target is None:
                raise AssertionError("fixture never blocked the active ascend")
            if delayed is None or delayed.data.get("stopped_reason") != "world_changed":
                raise AssertionError(f"blocked ascend did not enter delayed cancellation: {events}")
            if move_done is None or move_done.data.get("stopped_reason") != "world_changed":
                raise AssertionError(f"blocked ascend did not produce typed moveDone: {events}")
            if navigate_done is None or navigate_done.data.get("reason") != "world_changed":
                raise AssertionError(f"blocked ascend did not produce typed navigateDone: {events}")
            if not (delayed.seq < move_done.seq < navigate_done.seq):
                raise AssertionError(f"terminal ordering was not delayed-cancel then move/navigate terminal: {events}")
            if head["owner"] is not None:
                raise AssertionError(f"owner remained after blocked ascend terminal: {head}")
            if not on_ground:
                raise AssertionError("blocked ascend terminal returned before the Body settled on support")
            blocked = body.perceive(
                "blockAt",
                {"x": body.injected_target[0], "y": body.injected_target[1], "z": body.injected_target[2]},
            )
            if not (blocked.ok and blocked.complete and str(blocked.data.get("type", "")).endswith("stone")):
                raise AssertionError(f"fixture obstruction was unexpectedly mutated: {blocked}")
            print(
                {
                    "reason": result.reason,
                    "injected_target": body.injected_target,
                    "delayed_seq": delayed.seq,
                    "move_done_seq": move_done.seq,
                    "navigate_done_seq": navigate_done.seq,
                    "owner": head["owner"],
                }
            )
        finally:
            command(rcon, f"player {BOT} kill")


if __name__ == "__main__":
    main()
