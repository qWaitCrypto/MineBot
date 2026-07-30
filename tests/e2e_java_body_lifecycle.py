#!/usr/bin/env python3
"""Bounded Java-only FakePlayer death, respawn, and continuation proof."""

from __future__ import annotations

import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.body import LifecycleTransactions
from minebot.contract import Action, Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "javalifecycle"
BODY_URL = "ws://127.0.0.1:8767"
NATURAL = Region("java-lifecycle", (-16, -128, -16), (16, 320, 16))


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    response = rcon.command(value)
    if delay:
        time.sleep(delay)
    return response


def wait_for_events(body, names: set[str], *, timeout_s: float = 12.0):
    deadline = time.monotonic() + timeout_s
    seen = {}
    while time.monotonic() < deadline:
        for event in body.poll_events():
            if event.name in names and event.name not in seen:
                seen[event.name] = event
        if names <= seen.keys():
            return seen
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {sorted(names - seen.keys())}")


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    provider = build_body_provider(
        "java", bot_name=BOT, natural_region=NATURAL, java_body_url=BODY_URL
    )
    assert provider.java_body is not None
    body = provider.body

    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        command(rcon, "gamerule keepInventory false")
        command(rcon, "fill -2 58 -2 8 58 2 stone")
        command(rcon, "fill -2 59 -2 8 82 2 air")

        body.despawn()
        head = body.event_head("java-lifecycle-live")
        body.last_seq = int(head["event_seq"])

        spawned = body.spawn((0, 59, 0), gamemode="survival", timeout_s=10.0)
        assert spawned.ok and spawned.accepted, spawned
        command(rcon, f"item replace entity {BOT} hotbar.0 with bread 2")
        baseline_state = body.get_state()
        read_client = provider.java_body._read_client
        abandoned = read_client.protocol.inventory(BOT, start=0, limit=46)
        read_client._send(abandoned)
        state_before = body.get_state()
        assert not state_before.missing
        assert state_before.health == baseline_state.health
        assert math.dist(state_before.pos, baseline_state.pos) <= 0.01
        assert state_before.inventory_counts.get("minecraft:bread") == 2

        command(rcon, f"tp {BOT} 0 -80 0", delay=0.2)
        lifecycle_events = wait_for_events(body, {"death", "bodyMissing"})
        state_dead = body.get_state()
        assert state_dead.missing
        death = lifecycle_events["death"]
        missing = lifecycle_events["bodyMissing"]
        assert death.data.get("inventory_hash") == state_before.inventory_hash
        assert death.data.get("inventory_counts_before", {}).get("minecraft:bread") == 2
        assert missing.data.get("reason") == "death"

        recovered = LifecycleTransactions(body).recover_after_death(
            respawn_pos=(3, 59, 0),
            yaw=90.0,
            pitch=0.0,
            gamemode="survival",
            spawn_timeout_s=10.0,
            respawn_event_timeout_s=6.0,
        )
        assert recovered.success, recovered
        state_recovered = body.get_state()
        assert not state_recovered.missing
        assert math.dist(state_recovered.pos, (3.0, 59.0, 0.0)) <= 1.0

        continued = body.execute(Action.create("navigate", {
            "goal": {"kind": "near", "x": 5, "y": 59, "z": 0, "range": 1.0},
            "timeout_ticks": 400,
        }))
        assert continued.ok and continued.accepted, continued
        state_after_move = body.get_state()
        assert math.dist(state_after_move.pos, (5.0, 59.0, 0.0)) <= 1.5

        command(rcon, "fill 5 58 0 64 58 0 stone")
        command(rcon, "fill 5 59 0 64 61 0 air")
        with ThreadPoolExecutor(max_workers=1) as pool:
            moving = pool.submit(body.execute, Action.create("navigate", {
                "goal": {"kind": "near", "x": 60, "y": 59, "z": 0, "range": 1.0},
                "timeout_ticks": 1200,
            }))
            time.sleep(0.4)
            interrupted = body.interrupt("bounded_lifecycle_probe")
            canceled = moving.result(timeout=10.0)
        owner_after_cancel = body.event_head("java-lifecycle-cancel").get("owner")
        assert interrupted.ok and interrupted.accepted, interrupted
        assert not canceled.ok and canceled.error == "cancel_requested", canceled
        assert owner_after_cancel is None

        artifact = {
            "scope": "java_body_lifecycle",
            "formal_gate": False,
            "bounded": True,
            "body_provider": "java",
            "rcon_role": "fixture_setup_and_forced_death_only",
            "spawn": {
                "ok": spawned.ok,
                "final_pos": spawned.data.get("final_pos"),
            },
            "read_correlation": {
                "abandoned_request_id": abandoned.get("request_id"),
                "health": state_before.health,
                "position_matches": math.dist(state_before.pos, baseline_state.pos) <= 0.01,
            },
            "death": {
                "event_pos": death.data.get("pos"),
                "inventory_hash_matches": death.data.get("inventory_hash") == state_before.inventory_hash,
                "bread_before": death.data.get("inventory_counts_before", {}).get("minecraft:bread"),
                "missing_after": state_dead.missing,
                "missing_reason": missing.data.get("reason"),
            },
            "respawn": {
                "reason": recovered.reason,
                "final_pos": list(state_recovered.pos),
            },
            "continuation": {
                "action_ok": continued.ok,
                "final_pos": list(state_after_move.pos),
            },
            "interrupt": {
                "accepted": interrupted.accepted,
                "action_error": canceled.error,
                "owner_after": owner_after_cancel,
            },
            "scarpet_body_constructed": False,
        }
        out = Path("logs/agentic-runtime/java-body-lifecycle-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            body.despawn()
        except Exception:
            pass
        provider.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
