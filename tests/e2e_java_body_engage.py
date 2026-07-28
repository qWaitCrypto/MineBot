#!/usr/bin/env python3
"""Bounded Java-only proof for canonical hostile melee engagement."""

from __future__ import annotations

import json
import sys
import time
from math import dist
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaEngage"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("java-engage-probe", (-8, 190, -8), (28, 220, 8))
TARGET = (10, 200, 0)


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    response = rcon.command(value)
    if delay:
        time.sleep(delay)
    return response


def wait_for_presence(body, *, present: bool, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if body.get_state().missing == (not present):
            return
        time.sleep(0.1)
    raise AssertionError(f"FakePlayer presence did not become {present}")


def teleport(rcon: RconClient, body, pos: tuple[int, int, int]) -> tuple[float, float, float]:
    command(rcon, f"tp {BOT} {pos[0]} {pos[1]} {pos[2]}")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        observed = body.get_state().pos
        if dist(observed, pos) < 1.0:
            return observed
        time.sleep(0.1)
    raise AssertionError(f"teleport did not settle at {pos}: {body.get_state().pos}")


def target_present(rcon: RconClient) -> bool:
    result = command(
        rcon,
        "execute as @e[type=minecraft:husk,tag=java_engage_target,limit=1] "
        "run data get entity @s Pos",
        delay=0.0,
    )
    return "has the following entity data" in result.lower()


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=REGION,
        java_body_url=BODY_URL,
    )
    assert provider.scarpet_body is None
    assert provider.java_body is not None
    body = provider.body
    action_names: list[str] = []
    original_execute = body.execute

    def record_execute(action):
        action_names.append(action.name)
        return original_execute(action)

    body.execute = record_execute  # type: ignore[method-assign]
    registry = build_phase1_registry(
        body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=provider.governance,
        ),
    )

    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        command(rcon, "difficulty normal")
        command(rcon, "gamerule doMobSpawning false")
        command(rcon, f"player {BOT} kill")
        command(rcon, "kill @e[type=minecraft:husk]")
        command(rcon, "fill -4 200 -8 24 210 8 air")
        command(rcon, "fill -4 199 -8 24 199 8 stone")
        command(
            rcon,
            f"summon minecraft:husk {TARGET[0]} {TARGET[1]} {TARGET[2]} "
            "{NoAI:1b,PersistenceRequired:1b,Health:20f,Tags:[\"java_engage_target\"]}",
        )
        assert target_present(rcon), "controlled hostile did not spawn"
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with minecraft:diamond_sword 1")
        command(rcon, f"player {BOT} hotbar 1")
        start = teleport(rcon, body, (0, 200, 0))

        result = registry.get("engage_entity").callable(
            {
                "target": "nearest_hostile",
                "attack_range": 2.0,
                "cooldown_ticks": 10,
                "timeout_s": 20.0,
                "disengage_health": 6.0,
            }
        )
        end = body.get_state().pos

        assert result.success and result.reason == "killed", result
        assert int((result.metrics or {}).get("attacks") or 0) > 0, result
        assert (result.metrics or {}).get("target_id"), result
        assert (result.metrics or {}).get("target_health") == 0.0, result
        assert (result.metrics or {}).get("damage_observed") is True, result
        assert int((result.metrics or {}).get("min_attack_interval_ticks") or 0) >= 10, result
        assert dist(start, end) > 4.0, (start, end, result)
        assert not target_present(rcon), result
        assert action_names == ["engageEntity"], action_names

        artifact = {
            "scope": "java_body_engage",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "provider": "java",
            "scarpet_body_constructed": False,
            "scarpet_action_calls": 0,
            "provider_actions": action_names,
            "start": list(start),
            "end": list(end),
            "moved_distance": dist(start, end),
            "result": {
                "reason": result.reason,
                "success": result.success,
                "metrics": result.metrics,
            },
            "target_present_after": target_present(rcon),
        }
        output = Path("logs/agentic-runtime/java-body-engage-20260728.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            command(rcon, f"player {BOT} kill")
            wait_for_presence(body, present=False)
            command(rcon, "difficulty peaceful")
        except Exception:
            pass
        provider.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
