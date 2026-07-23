#!/usr/bin/env python3
"""Q4t live fault injection for one mutating Body dispatch.

The wrapper lets the real RCON request reach Scarpet, then drops only its
response.  ``ScarpetBody`` must reconcile the immutable action id before it
can return, and the authoritative block/event facts must show one mutation.
This is a bounded proof; it is not a Q5 long run.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.contract import Action  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "Q4tFaultProbe"
TARGET = (1, 64, 0)


class DropAfterServerResponse:
    def __init__(self, inner: RconClient) -> None:
        self.inner = inner
        self.dropped = False

    def request_once(self, command: str) -> str:
        if "minebot_action" in command and "minebot_action_status" not in command and not self.dropped:
            if self.inner._sock is None:  # noqa: SLF001 - fault injection owns the real transport
                self.inner.connect()
            self.inner._sock = _DropResponseSocket(self.inner._sock)  # noqa: SLF001
            self.dropped = True
        return self.inner.request_once(command)

    def request(self, command: str) -> str:
        return self.inner.request(command)

    def reconnect(self) -> None:
        self.inner.reconnect()


class _DropResponseSocket:
    """Close the real socket after sendall, before RCON reads the response."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.armed = True

    def sendall(self, data: bytes) -> None:
        self.inner.sendall(data)

    def recv(self, size: int) -> bytes:
        if self.armed:
            self.armed = False
            self.inner.close()
            return b""
        return self.inner.recv(size)

    def settimeout(self, value: float) -> None:
        self.inner.settimeout(value)

    def close(self) -> None:
        self.inner.close()


def command(rcon: RconClient, text: str, delay: float = 0.05) -> str:
    result = rcon.command(text)
    if delay:
        time.sleep(delay)
    return result


def setup_world(rcon: RconClient) -> None:
    for text in (
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
        f"player {BOT} kill",
        "fill -4 63 -4 4 68 4 air",
        "fill -4 62 -4 4 62 4 stone",
        f"setblock {TARGET[0]} {TARGET[1]} {TARGET[2]} stone",
        "script in minebot run minebot_reset()",
    ):
        command(rcon, text)


def main() -> None:
    with connect_or_skip(RconConfig()) as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, DropAfterServerResponse(rcon))
        try:
            spawn_or_fail(body, (0, 64, 0), timeout_s=10.0)
            command(rcon, f"gamemode survival {BOT}")
            command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
            body.poll_events()

            action = Action(
                id=f"q4t-fault-{uuid4()}",
                name="mineBlock",
                params={"target": list(TARGET), "block_type": "stone", "timeout_ticks": 100},
            )
            result = body.execute(action)
            terminal = body.await_action_terminal(action.id, timeout_s=12.0)
            fact = body.perceive("blockAt", {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]})
            matching = [
                event
                for event in body.event_log
                if event.data.get("action_id") == action.id and event.name == "mineDone"
            ]
            if not (result.ok and result.accepted):
                raise AssertionError({"dispatch": result})
            if terminal.data.get("success") is not True or fact.data.get("type") not in {"air", "minecraft:air"}:
                raise AssertionError({"terminal": terminal.data, "block": fact.data})
            if len(matching) != 1:
                raise AssertionError({"matching_terminal_events": len(matching), "events": body.event_log})

            print(
                json.dumps(
                    {
                        "action_id": action.id,
                        "simulated_response_drop": True,
                        "terminal": terminal.name,
                        "terminal_data": terminal.data,
                        "block_fact": fact.data,
                        "matching_terminal_events": len(matching),
                        "trace": body.observability_snapshot()["action_traces"][-1],
                        "transport": rcon.stats_snapshot(),
                        "scope": "Q4t_part1_action_reconciliation",
                    },
                    sort_keys=True,
                )
            )
        finally:
            rcon.command(f"player {BOT} kill")


if __name__ == "__main__":
    main()
