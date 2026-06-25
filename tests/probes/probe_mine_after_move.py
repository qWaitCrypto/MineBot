#!/usr/bin/env python3
"""Runtime probe for the moveTo -> mineBlock transaction seam."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minebot.contract import Action
from minebot.game import RconClient, ScarpetBody
from minebot.game.rcon import RconConfig


BOT = "ProbeMineBot"
TARGET = (110, 70, 3)


def command(rcon: RconClient, cmd: str, delay: float = 0.05) -> str:
    out = rcon.command(cmd)
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
        "fill 104 70 -2 116 76 6 air",
        "fill 104 69 -2 116 69 6 stone",
        f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} stone",
    ]:
        command(rcon, cmd)


def dump(label: str, payload) -> None:
    print(f"\n## {label}")
    if is_dataclass(payload):
        payload = asdict(payload)
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    rcon = RconClient(RconConfig())
    rcon.connect()
    with rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        body.spawn((110, 70, 0))
        command(rcon, f"tp {BOT} 110 70 0 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")
        command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
        command(rcon, "script in minebot run minebot_reset()")

        state0 = body.get_state()
        dump("state_before", state0)

        move = Action.create(
            "moveTo",
            {
                "target": [110.5, 70.0, 2.5],
                "arrival_radius": 0.15,
                "timeout_ticks": 160,
                "no_progress_ticks": 60,
                "max_deviation": 2.0,
            },
        )
        dump("move_execute", body.execute(move))
        move_term = body.await_action_terminal(move.id, timeout_s=15.0)
        dump("move_done", move_term)
        dump("events_after_move", body.poll_events())

        state1 = body.get_state()
        dump("state_after_move", state1)

        time.sleep(0.3)
        state2 = body.get_state()
        dump("state_after_settle_0_3", state2)

        mine = Action.create(
            "mineBlock",
            {
                "target": list(TARGET),
                "block_type": "stone",
                "context": "travel",
                "legality": {
                    "allowed": True,
                    "reason": "allowed_natural",
                    "protected": False,
                    "bot_owned": False,
                    "natural_region": "e2e-mine",
                    "details": {},
                },
            },
        )
        dump("mine_execute", body.execute(mine))

        start = time.time()
        while time.time() - start < 10.0:
            time.sleep(0.5)
            state = body.get_state()
            block = body.perceive("blockAt", {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]})
            events = body.poll_events()
            dump(
                f"tick_{round(time.time() - start, 1)}",
                {
                    "state": state.to_payload(),
                    "block": asdict(block),
                    "events": [asdict(event) for event in events],
                },
            )
            done = [event for event in events if event.name == "mineDone" and event.data.get("action_id") == mine.id]
            if done:
                break

        command(rcon, "script in minebot run minebot_reset()")


if __name__ == "__main__":
    main()
