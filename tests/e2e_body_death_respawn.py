#!/usr/bin/env python3
"""Death/respawn lifecycle e2e against the local Carpet test server."""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import LifecycleTransactions
from minebot.contract import Action
from minebot.game import RconClient, ScarpetBody
from tests.e2e_support import connect_or_skip


BOT = "E2ERespawnBot"


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
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "script in minebot run minebot_reset()",
        "script in minebot run global_reflex_scan = false",
    ]:
        command(rcon, cmd)


def wait_for_event(body: ScarpetBody, name: str, *, timeout_s: float = 12.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in body.poll_events():
            if event.name == name:
                return event
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for event {name}")


def wait_for_events(body: ScarpetBody, names: set[str], *, timeout_s: float = 12.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    seen: dict[str, object] = {}
    while time.monotonic() < deadline:
        for event in body.poll_events():
            if event.name in names and event.name not in seen:
                seen[event.name] = event
        if all(name in seen for name in names):
            return seen
        time.sleep(0.05)
    missing = sorted(name for name in names if name not in seen)
    raise AssertionError(f"timed out waiting for events {missing}")


def run_death_respawn_happy_path(rcon: RconClient, body: ScarpetBody) -> None:
    spawn = body.spawn((0, 80, 0), timeout_s=10.0)
    if not (spawn.ok and spawn.accepted):
        raise AssertionError(f"initial spawn failed: {spawn}")
    lifecycle = LifecycleTransactions(body)
    not_missing = lifecycle.recover_after_death(respawn_pos=(3, 59, 0), yaw=90.0, pitch=0.0)
    if not_missing.success or not_missing.reason != "body_not_missing":
        raise AssertionError(f"recover_after_death should refuse when body is present: {not_missing}")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with bread 2")
    state = body.get_state()
    if state.missing:
        raise AssertionError(f"body unexpectedly missing after spawn: {state}")
    if "bread" not in state.inventory_raw:
        raise AssertionError(f"expected pre-death inventory seed missing from state: {state.inventory_raw}")

    command(rcon, f"tp {BOT} 0 -80 0", delay=0.2)
    events = wait_for_events(body, {"death", "bodyMissing"}, timeout_s=12.0)
    death = events["death"]
    missing_event = events["bodyMissing"]
    state_after_death = body.get_state()
    if not state_after_death.missing:
        raise AssertionError(f"body should be missing after death: {state_after_death}")
    if "pos" not in death.data or "inventory_hash" not in death.data:
        raise AssertionError(f"death event missing expected facts: {death}")
    if "lastPos" not in missing_event.data:
        raise AssertionError(f"bodyMissing event missing lastPos: {missing_event}")
    if death.data.get("inventory_hash") != state.inventory_hash:
        raise AssertionError(
            f"death event inventory hash drifted from pre-death authoritative state: "
            f"event={death.data.get('inventory_hash')} state={state.inventory_hash}"
        )
    inventory_before = str(death.data.get("inventory_before") or "")
    if "bread" not in inventory_before:
        raise AssertionError(f"death event inventory_before lost pre-death item truth: {death.data}")

    action = Action.create("moveTo", {"target": [1, 59, 0]})
    result = body.execute(action)
    if not result.ok or result.accepted:
        raise AssertionError(f"missing-body moveTo should fail honestly at dispatch: {result}")

    recovered = lifecycle.recover_after_death(respawn_pos=(3, 59, 0), yaw=90.0, pitch=0.0)
    if not recovered.success:
        raise AssertionError(f"recover_after_death failed: {recovered}")
    final_state = body.get_state()
    if final_state.missing:
        raise AssertionError(f"body still missing after recovery: {final_state}")
    if math.dist(final_state.pos, (3.0, 59.0, 0.0)) > 1.0:
        raise AssertionError(f"recovery landed too far from requested position: {final_state.pos}")
    respawned = recovered.metrics.get("respawned_event") if recovered.metrics else None
    if not isinstance(respawned, dict):
        raise AssertionError(f"recover_after_death missing respawned_event metrics: {recovered}")
    if math.dist(tuple(respawned.get("final_pos") or ()), (3.0, 59.0, 0.0)) > 1.0:
        raise AssertionError(f"respawned event reported wrong position: {respawned}")

    continue_action = Action.create("moveTo", {"target": [5, 59, 0]})
    continue_result = body.execute(continue_action)
    if not continue_result.ok or not continue_result.accepted:
        raise AssertionError(f"post-recovery moveTo should be accepted: {continue_result}")
    terminal = body.await_action_terminal(continue_action.id, timeout_s=10.0)
    if terminal.name != "moveDone" or not terminal.data.get("arrived"):
        raise AssertionError(f"post-recovery moveTo did not complete truthfully: {terminal}")
    continued_state = body.get_state()
    if math.dist(continued_state.pos, (5.0, 59.0, 0.0)) > 1.0:
        raise AssertionError(f"post-recovery continuation landed too far from target: {continued_state.pos}")


def main() -> int:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        try:
            run_death_respawn_happy_path(rcon, body)
        finally:
            command(rcon, f"player {BOT} kill", delay=0.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
