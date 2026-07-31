#!/usr/bin/env python3
"""Replay the formal run's natural cave recovery through the Java-only registry."""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import time

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "Bot1"
BODY_URL = "ws://127.0.0.1:8767"
START = (-96.49821104903306, 65.0, 58.30000001192093)
NATURAL = Region("formal-world", (-256, -128, -256), (256, 320, 256))
PROBE_PLANKS = (
    (-98, 65, 61),
    (-98, 67, 62),
    (-102, 70, 65),
    (-102, 71, 65),
    (-102, 72, 65),
    (-102, 73, 65),
    (-106, 76, 66),
    (-106, 77, 66),
)
LATEST_BLOCKER = (-106, 80, 67)
ARTIFACT = Path("logs/agentic-runtime/java-body-formal-cave-replay-20260730.json")


def wait_for_player(rcon: RconClient) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "No entity was found" not in rcon.command(f"data get entity {BOT} Pos"):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{BOT} did not join the test world")


def wait_for_position(body, expected: tuple[float, float, float]) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        state = body.get_state()
        if not state.missing and math.dist(state.pos, expected) <= 0.25:
            return
        time.sleep(0.05)
    raise AssertionError(f"{BOT} did not settle at replay start: {body.get_state().pos}")


def restore_previous_replay(rcon: RconClient) -> None:
    if not ARTIFACT.exists():
        return
    try:
        previous = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    attempts = previous.get("surface_attempts")
    if not isinstance(attempts, list):
        attempts = [previous.get("result")]
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        metrics = attempt.get("metrics")
        if not isinstance(metrics, dict):
            continue
        for broken in metrics.get("broken") or []:
            if not isinstance(broken, dict):
                continue
            block_id = str(broken.get("block_id") or "")
            if not block_id:
                continue
            rcon.command(
                f"setblock {int(broken['x'])} {int(broken['y'])} {int(broken['z'])} {block_id}"
            )
        for placed in metrics.get("placed") or []:
            if not isinstance(placed, dict):
                continue
            rcon.command(
                f"setblock {int(placed['x'])} {int(placed['y'])} {int(placed['z'])} air"
            )


def prepare_replay(rcon: RconClient) -> None:
    rcon.command(f"player {BOT} spawn")
    wait_for_player(rcon)
    rcon.command(f"gamemode spectator {BOT}")
    rcon.command(f"clear {BOT}")
    rcon.command(f"effect clear {BOT}")
    restore_previous_replay(rcon)
    for x, y, z in PROBE_PLANKS:
        rcon.command(f"setblock {x} {y} {z} air")
    rcon.command("setblock -97 64 58 stone")
    rcon.command("setblock -97 65 58 air")
    rcon.command(f"tp {BOT} {START[0]} {START[1]} {START[2]}")
    rcon.command(f"gamemode survival {BOT}")
    rcon.command(f"give {BOT} spruce_planks 21")
    rcon.command(f"give {BOT} stone_pickaxe 1")


def main() -> int:
    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        prepare_replay(rcon)
        provider = build_body_provider(
            "java",
            bot_name=BOT,
            natural_region=NATURAL,
            java_body_url=BODY_URL,
        )
        body = provider.body
        parts = build_phase1_agent_runtime(
            body=body,
            goal_text="return to the surface",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=NATURAL,
                body_provider="java",
                governance_policy=provider.governance,
            ),
            agent_name="JavaFormalCaveReplay",
        )
        try:
            wait_for_position(body, START)
            head = body.event_head("java-formal-cave-replay")
            body.last_seq = int(head["event_seq"])
            start_seq = body.last_seq
            before = body.get_state()
            started = time.monotonic()
            attempts = []
            confirmations = []
            seen_challenges: set[str] = set()
            for _attempt in range(4):
                result = parts.registry.get("go_to_surface").callable({"timeout_s": 60})
                attempts.append(result.to_payload())
                if result.success:
                    break
                metrics = result.metrics or {}
                challenges = []
                primary = metrics.get("governance_confirmation")
                if isinstance(primary, dict):
                    challenges.append(primary)
                additional = metrics.get("additional_governance_confirmations")
                if isinstance(additional, list):
                    challenges.extend(item for item in additional if isinstance(item, dict))
                fresh = [
                    challenge
                    for challenge in challenges
                    if isinstance(challenge.get("challenge_id"), str)
                    and challenge["challenge_id"] not in seen_challenges
                ]
                if result.reason != "governance_confirmation_required" or not fresh:
                    break
                for challenge in fresh:
                    challenge_id = str(challenge["challenge_id"])
                    seen_challenges.add(challenge_id)
                    confirmed = parts.registry.get("confirm_governance_scope").callable(
                        {
                            "challenge_id": challenge_id,
                            "purpose": "continue bounded natural-stone recovery to a mobile surface stand",
                        }
                    )
                    confirmations.append(confirmed.to_payload())
            wall_s = time.monotonic() - started
            body.poll_events()
            after = body.get_state()
            events = [event for event in body.event_log if event.seq > start_seq]
        finally:
            body.interrupt("bounded_probe_cleanup")

    proposal_positions = Counter(
        (int(event.data["x"]), int(event.data["y"]), int(event.data["z"]))
        for event in events
        if event.name in {"ascent_break_proposed", "ascent_continuation_preflight_proposed"}
    )
    ascent_events = [
        {
            "seq": event.seq,
            "tick": event.tick,
            "name": event.name,
            "data": dict(event.data),
        }
        for event in events
        if event.name.startswith("ascent_") or event.name == "action_terminal"
    ]
    before_planks = int((before.inventory_counts or {}).get("minecraft:spruce_planks", 0))
    after_planks = int((after.inventory_counts or {}).get("minecraft:spruce_planks", 0))
    latest_blocker_proposals = proposal_positions[LATEST_BLOCKER]
    artifact = {
        "scope": "java_body_formal_cave_replay",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "scarpet_constructed": provider.scarpet_body is not None,
        "start_pos": list(before.pos),
        "end_pos": list(after.pos),
        "elapsed_wall_s": round(wall_s, 3),
        "height_delta": round(after.pos[1] - before.pos[1], 3),
        "spruce_planks_before": before_planks,
        "spruce_planks_after": after_planks,
        "spruce_planks_consumed": before_planks - after_planks,
        "latest_blocker_proposals": latest_blocker_proposals,
        "surface_attempts": attempts,
        "governance_confirmations": confirmations,
        "result": result.to_payload(),
        "ascent_events": ascent_events,
    }
    artifact["success"] = bool(
        result.success
        and result.reason == "surface_reached"
        and after.pos[1] > before.pos[1]
        and 1 <= latest_blocker_proposals <= 2
        and bool(confirmations)
        and all(entry.get("success") is True for entry in confirmations)
        and provider.scarpet_body is None
    )
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if artifact["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
