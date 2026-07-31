#!/usr/bin/env python3
"""Replay run 8's false excavation terminal through canonical collection."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "Bot1"
BODY_URL = "ws://127.0.0.1:8767"
RUN8_COLLECT_START = (-78.47586685700442, 71.0, -20.650794580135646)
RUN8_PRIOR_MINED_BLOCKS = (
    *((-79, y, -21) for y in range(71, 76)),
    (-74, 70, -21),
    (-73, 70, -21),
    (-72, 71, -20),
)
NATURAL = Region("run8-world", (-256, -128, -256), (256, 320, 256))
ARTIFACT = Path("logs/agentic-runtime/java-body-run8-coal-collect-replay-20260731.json")


def wait_for_position(body, expected: tuple[float, float, float], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if math.dist(body.get_state().pos, expected) <= 0.25:
            return
        time.sleep(0.05)
    raise AssertionError(f"Bot1 did not settle at replay start: {body.get_state().pos}")


def wait_for_item(body, item: str, minimum: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if int((body.get_state().inventory_counts or {}).get(item, 0)) >= minimum:
            return
        time.sleep(0.05)
    raise AssertionError(f"Bot1 did not receive {item}: {body.get_state().inventory_counts}")


def event_payload(event) -> dict[str, object]:
    return {
        "seq": event.seq,
        "tick": event.tick,
        "name": event.name,
        "data": dict(event.data),
    }


def main() -> int:
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=NATURAL,
        java_body_url=BODY_URL,
    )
    body = provider.body
    parts = build_phase1_agent_runtime(
        body=body,
        goal_text="collect 1 coal",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name="JavaRun8CollectReplay",
    )

    try:
        with RconClient(
            RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
        ) as rcon:
            state = body.get_state()
            if state.missing:
                spawned = body.spawn(RUN8_COLLECT_START, gamemode="survival", timeout_s=10.0)
                if not spawned.ok:
                    raise AssertionError(f"Java Body could not spawn replay player: {spawned}")
            rcon.command(f"clear {BOT}")
            rcon.command(f"effect clear {BOT}")
            rcon.command(f"effect give {BOT} minecraft:instant_health 1 10 true")
            rcon.command(f"gamemode survival {BOT}")
            for x, y, z in RUN8_PRIOR_MINED_BLOCKS:
                rcon.command(f"setblock {x} {y} {z} air")
            rcon.command(
                f"tp {BOT} {RUN8_COLLECT_START[0]} {RUN8_COLLECT_START[1]} {RUN8_COLLECT_START[2]}"
            )
            rcon.command(f"give {BOT} minecraft:stone_pickaxe 1")
            wait_for_position(body, RUN8_COLLECT_START, 5.0)
            wait_for_item(body, "minecraft:stone_pickaxe", 1, 5.0)

            equipped = parts.registry.get("equip_item").callable(
                {"item": "stone_pickaxe", "target": "mainhand"}
            )
            if not equipped.success:
                raise AssertionError(f"stone pickaxe setup failed: {equipped.to_payload()}")

            head = body.event_head("java-run8-collect-replay")
            start_seq = int(head["event_seq"])
            body.last_seq = start_seq
            before = body.get_state()
            started = time.monotonic()
            result = parts.registry.get("collect_resource").callable(
                {
                    "item": "coal",
                    "count": 1,
                    "constraints": {
                        "auto_prerequisites": False,
                        "radius": 32,
                        "max_candidates": 8,
                        "max_mutating_calls": 8,
                        "max_wall_s": 120,
                    },
                }
            )
            elapsed = time.monotonic() - started
            body.poll_events()
            after = body.get_state()

        before_coal = int((before.inventory_counts or {}).get("minecraft:coal", 0))
        after_coal = int((after.inventory_counts or {}).get("minecraft:coal", 0))
        events = [event for event in body.event_log if event.seq > start_seq]
        action_terminals = [
            event_payload(event)
            for event in events
            if event.name == "action_terminal"
        ]
        artifact = {
            "scope": "java_body_run8_coal_collect_replay",
            "formal_gate": False,
            "bounded": True,
            "production_provider": "java",
            "scarpet_constructed": provider.scarpet_body is not None,
            "start_pos": list(before.pos),
            "end_pos": list(after.pos),
            "before_inventory": before.inventory_counts or {},
            "after_inventory": after.inventory_counts or {},
            "elapsed_wall_s": round(elapsed, 3),
            "result": result.to_payload(),
            "coal_delta": after_coal - before_coal,
            "events": [event_payload(event) for event in events],
            "action_terminals": action_terminals,
            "success": bool(
                result.success
                and after_coal > before_coal
                and "terminal_interaction_unsatisfied" not in result.reason
                and provider.scarpet_body is None
            ),
        }
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0 if artifact["success"] else 1
    finally:
        parts.runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
