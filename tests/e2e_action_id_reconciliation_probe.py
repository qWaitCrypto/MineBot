#!/usr/bin/env python3
"""Bounded single-writer probe for an accepted-but-unacknowledged action.

The wrapper drops the first dispatch response after the real RCON request has
reached Scarpet.  Replaying the same Action object then exercises Scarpet's
action-id cache and the normal terminal-event wait without replaying a second
physical action.

This is supporting transport evidence only.  It does not prove cache
eviction, app reload recovery, or long-run server-load stability.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.contract import Action
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import connect_or_skip, spawn_or_fail


class _DropFirstActionResponse:
    """Drop exactly one already-served action response at the client edge."""

    def __init__(self, inner: RconClient) -> None:
        self.inner = inner
        self.dropped = False

    def request(self, command: str) -> str:
        raw = self.inner.request(command)
        if "minebot_action" in command and not self.dropped:
            self.dropped = True
            raise RconError("simulated lost response after server acceptance")
        return raw


def main() -> None:
    bot = "IdemDropProbe"
    action = Action(
        id=f"idem-drop-probe-{uuid4()}",
        name="selectSlot",
        params={"slot": 0},
    )

    with connect_or_skip(RconConfig()) as rcon:
        rcon.command(f"player {bot} kill")
        rcon.command("carpet allowSpawningOfflinePlayers true")
        body = ScarpetBody(bot, _DropFirstActionResponse(rcon))
        try:
            spawn_or_fail(body, (0, 64, 0), timeout_s=10.0)
            body.poll_events()

            first_error: str | None = None
            try:
                body.execute(action)
            except RconError as exc:
                first_error = str(exc)
            if first_error is None:
                raise AssertionError("the response-drop wrapper did not fire")

            replay = body.execute(action)
            if not (replay.ok and replay.accepted):
                raise AssertionError(f"same-id replay was not accepted: {replay}")
            terminal = body.await_action_terminal(action.id, timeout_s=5.0)
            matching = [
                event
                for event in body.event_log
                if event.name == "selectSlotDone" and event.data.get("action_id") == action.id
            ]
            if len(matching) != 1:
                raise AssertionError(f"expected one terminal event, got {len(matching)}")
            if terminal.data.get("stopped_reason") != "completed":
                raise AssertionError(f"unexpected terminal: {terminal.data}")

            print(
                {
                    "bot": bot,
                    "action_id": action.id,
                    "simulated_first_error": first_error,
                    "replay_ok": replay.ok,
                    "replay_accepted": replay.accepted,
                    "terminal": terminal.name,
                    "terminal_reason": terminal.data.get("stopped_reason"),
                    "matching_terminal_events": len(matching),
                    "transport_stats": rcon.stats_snapshot(),
                    "scope": "bounded_action_id_cache_only",
                }
            )
        finally:
            rcon.command(f"player {bot} kill")


if __name__ == "__main__":
    main()
