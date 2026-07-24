#!/usr/bin/env python3
"""Bounded transport sequence for the Q4t action handoff.

This is deliberately shorter than an autonomy gate.  It exercises two
independent response-loss boundaries on one real RCON session:

* fault A reaches the server and is reconciled to one applied mutation;
* a successful authoritative Body read separates the incidents;
* fault B loses its response while reconciliation reads are unavailable and
  therefore returns typed ``unknown`` without replaying the mutation;
* after reads are restored, the server-owned terminal and owner cleanup are
  observed exactly once.

The runner's consecutive-failure reset is covered by the focused runner unit
test.  This probe supplies the missing sequence-level live evidence and does
not authorize a long rehearsal by itself.
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
from minebot.game.errors import ActionReconciliationUnknownError, RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "Q4tSeqProbe"
TARGET_A = (1, 64, 0)
TARGET_B = (2, 64, 0)


class _DropAfterSendSocket:
    """Close the real socket after the request is sent, before its response."""

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


class _SequenceTransport:
    """Inject only the two faults needed by this bounded sequence."""

    def __init__(self, inner: RconClient) -> None:
        self.inner = inner
        self.drop_next_mutation_response = False
        self.fail_next_status_read = False

    def request_once(self, command: str) -> str:
        if self.drop_next_mutation_response and "minebot_action(" in command:
            if self.inner._sock is None:  # noqa: SLF001 - probe owns the fault boundary
                self.inner.connect()
            self.inner._sock = _DropAfterSendSocket(self.inner._sock)  # noqa: SLF001
            self.drop_next_mutation_response = False
        return self.inner.request_once(command)

    def request(self, command: str) -> str:
        if self.fail_next_status_read and "minebot_action_status" in command:
            self.fail_next_status_read = False
            raise RconError("simulated reconciliation read loss")
        return self.inner.request(command)

    def reconnect(self) -> None:
        self.inner.reconnect()

    def stats_snapshot(self) -> dict[str, int]:
        return self.inner.stats_snapshot()


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
        f"setblock {TARGET_A[0]} {TARGET_A[1]} {TARGET_A[2]} stone",
        f"setblock {TARGET_B[0]} {TARGET_B[1]} {TARGET_B[2]} air",
        f"setblock {TARGET_B[0]} {TARGET_B[1] - 1} {TARGET_B[2]} stone",
        "script in minebot run minebot_reset()",
    ):
        command(rcon, text)


def wait_for_cleanup(body: ScarpetBody, action_id: str, *, timeout_s: float = 8.0) -> tuple[object, list[object]]:
    deadline = time.monotonic() + timeout_s
    state = body.get_state()
    events = []
    while time.monotonic() < deadline:
        events.extend(body.poll_events())
        state = body.get_state()
        matching = [event for event in body.event_log if event.data.get("action_id") == action_id]
        if state.body_owner is None and (state.pending_action_count or 0) == 0 and matching:
            return state, matching
        time.sleep(0.1)
    return state, [event for event in body.event_log if event.data.get("action_id") == action_id]


def main() -> None:
    with connect_or_skip(RconConfig()) as rcon:
        setup_world(rcon)
        transport = _SequenceTransport(rcon)
        body = ScarpetBody(BOT, transport)
        try:
            spawn_or_fail(body, (0, 64, 0), timeout_s=10.0)
            command(rcon, f"gamemode survival {BOT}")
            command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
            body.event_head(f"q4t-sequence-{uuid4()}")
            body.poll_events()

            action_a = Action(
                id=f"q4t-seq-a-{uuid4()}",
                name="mineBlock",
                params={"target": list(TARGET_A), "block_type": "stone", "timeout_ticks": 100},
            )
            transport.drop_next_mutation_response = True
            result_a = body.execute(action_a)
            terminal_a = body.await_action_terminal(action_a.id, timeout_s=12.0)
            block_a = body.perceive("blockAt", {"x": TARGET_A[0], "y": TARGET_A[1], "z": TARGET_A[2]})
            matching_a = [
                event
                for event in body.event_log
                if event.data.get("action_id") == action_a.id and event.name == "mineDone"
            ]
            if not (result_a.ok and result_a.accepted and terminal_a.data.get("success") is True):
                raise AssertionError({"dispatch_a": result_a, "terminal_a": terminal_a.data})
            if block_a.data.get("type") not in {"air", "minecraft:air"} or len(matching_a) != 1:
                raise AssertionError({"block_a": block_a.data, "matching_a": len(matching_a)})

            # This successful authoritative read is the boundary that must
            # clear a consecutive transport-failure streak in the runner.
            state_between = body.get_state()
            if state_between.missing:
                raise AssertionError({"state_between": state_between})
            command(rcon, f"item replace entity {BOT} weapon.mainhand with stone 1")

            action_b = Action(
                id=f"q4t-seq-b-{uuid4()}",
                name="placeBlock",
                params={
                    "target": list(TARGET_B),
                    "block_type": "stone",
                    "face": "up",
                    "timeout_ticks": 100,
                },
            )
            transport.drop_next_mutation_response = True
            transport.fail_next_status_read = True
            unknown: dict[str, object] | None = None
            try:
                body.execute(action_b)
            except ActionReconciliationUnknownError as exc:
                unknown = dict(exc.diagnostics)
            if unknown is None:
                raise AssertionError("fault B did not produce typed action_reconciliation_unknown")

            # The read fault is now removed.  The action may have completed
            # despite the lost response; only authoritative state/event facts
            # decide whether owner cleanup and one terminal are present.
            time.sleep(0.25)
            state_after, matching_b = wait_for_cleanup(body, action_b.id)
            block_b = body.perceive("blockAt", {"x": TARGET_B[0], "y": TARGET_B[1], "z": TARGET_B[2]})
            matching_b = [
                event
                for event in body.event_log
                if event.data.get("action_id") == action_b.id and event.name == "placeDone"
            ]
            if state_after.body_owner is not None or (state_after.pending_action_count or 0) != 0:
                raise AssertionError({"state_after": state_after})
            if block_b.data.get("type") not in {"stone", "minecraft:stone"} or len(matching_b) != 1:
                raise AssertionError({"block_b": block_b.data, "matching_b": len(matching_b)})
            if matching_b[0].data.get("success") is not True:
                raise AssertionError({"terminal_b": matching_b[0].data})

            print(
                json.dumps(
                    {
                        "scope": "Q4t_bounded_transport_sequence",
                        "fault_a": {
                            "action_id": action_a.id,
                            "response_dropped_after_send": True,
                            "terminal": terminal_a.name,
                            "block": block_a.data,
                            "matching_terminal_events": len(matching_a),
                        },
                        "successful_authoritative_read_between_faults": {
                            "missing": state_between.missing,
                            "owner": state_between.body_owner,
                            "pending": state_between.pending_action_count,
                        },
                        "fault_b": {
                            "action_id": action_b.id,
                            "response_dropped_after_send": True,
                            "reconciliation_status": "unknown",
                            "diagnostics": unknown,
                            "terminal": matching_b[0].name,
                            "block": block_b.data,
                            "matching_terminal_events": len(matching_b),
                            "owner_after": state_after.body_owner,
                            "pending_after": state_after.pending_action_count,
                        },
                        "transport": transport.stats_snapshot(),
                        "body_trace": body.observability_snapshot(max_events=32, max_traces=8),
                    },
                    sort_keys=True,
                )
            )
        finally:
            try:
                rcon.command(f"player {BOT} kill")
            except Exception:
                pass


if __name__ == "__main__":
    main()
