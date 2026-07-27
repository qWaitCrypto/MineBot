#!/usr/bin/env python3
"""Bounded dry-land proof for Java entity reads and canonical search. Not a gate."""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "javaentityprobe"
GUIDE = "javaguide"
OBSERVER = "javaobserver"
BODY_URL = "ws://127.0.0.1:8767"
BASE = (60, 200, 60)
REGION = Region("java-entity-read", (48, 0, 48), (80, 320, 72))


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


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.dist(a, b)


def entities(body, **params) -> list[dict]:
    result = body.perceive("nearbyEntities", params)
    assert result.ok and result.complete, result
    return list(result.data.get("entities") or [])


def spawn_nearby_player(
    rcon: RconClient,
    body,
    name: str,
    pos: tuple[int, int, int],
    timeout_s: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        command(rcon, f"player {name} spawn at {pos[0]} {pos[1]} {pos[2]}")
        result = body.perceive(
            "nearbyEntities", {"radius": 16, "limit": 8, "types": ["player"], "name": name}
        )
        found = list(result.data.get("entities") or []) if result.ok else []
        if found:
            return found[0]
        time.sleep(0.1)
    raise AssertionError(f"nearby FakePlayer {name} did not become visible")


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=REGION,
        java_body_url=BODY_URL,
    )
    assert provider.java_body is not None
    registry = build_phase1_registry(
        provider.body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=provider.governance,
        ),
    )
    search = registry.get("search_for_entity")

    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        for player in (BOT, GUIDE, OBSERVER):
            command(rcon, f"player {player} kill")
        command(rcon, "kill @e[tag=minebot.entity.probe]")
        command(rcon, "kill @e[type=minecraft:item]")
        command(rcon, "fill 56 200 56 72 203 64 air")
        command(rcon, "fill 56 199 56 72 199 64 stone")

        command(rcon, f"player {BOT} spawn")
        wait_for_presence(provider.body, present=True)
        command(rcon, f"execute in minecraft:overworld run tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]}")
        spawn_nearby_player(rcon, provider.body, GUIDE, (BASE[0] + 3, BASE[1], BASE[2]))
        spawn_nearby_player(rcon, provider.body, OBSERVER, (BASE[0] + 2, BASE[1], BASE[2] + 1))
        command(rcon, f"tag {OBSERVER} add minebot.camera.observer")
        command(
            rcon,
            f"summon cow {BASE[0] + 5} {BASE[1]} {BASE[2]} "
            "{NoAI:1b,PersistenceRequired:1b,Tags:[\"minebot.entity.probe\"]}",
        )
        command(
            rcon,
            f"summon item {BASE[0] + 1.0} {BASE[1] + 0.2} {BASE[2] + 1.0} "
            "{NoGravity:1b,PickupDelay:32767s,Tags:[\"minebot.entity.probe\"],"
            "Item:{id:\"minecraft:oak_log\",count:1}}",
        )

        before = provider.body.get_state()
        started = time.monotonic()
        found = search.callable(
            {
                "entity_types": ["player"],
                "entity_name": GUIDE,
                "search_radius": 16,
                "min_distance": 0.0,
                "max_distance": 4.5,
                "vertical_tolerance": 1.5,
            }
        )
        search_wall_ms = (time.monotonic() - started) * 1000.0
        after_found = provider.body.get_state()
        assert found.success and found.reason == "entity_in_range", found.to_payload()
        assert found.metrics["target"]["name"] == GUIDE, found.to_payload()
        assert found.metrics["target"]["id"], found.to_payload()
        assert found.metrics["approach"]["navigated"] is False, found.to_payload()
        assert distance(before.pos, after_found.pos) < 0.25

        guide_first = entities(
            provider.body, radius=16, limit=8, types=["player"], name=GUIDE
        )[0]
        guide_second = entities(
            provider.body, radius=16, limit=8, types=["player"], name=GUIDE
        )[0]
        assert guide_first["id"] == guide_second["id"]
        assert guide_first["health"] == 20.0

        cows = entities(provider.body, radius=16, limit=1, types=["cow"])
        items = entities(provider.body, radius=16, limit=1, types=["item"])
        players = entities(provider.body, radius=16, limit=8, types=["player"])
        assert len(cows) == 1 and cows[0]["type"] == "minecraft:cow"
        assert len(items) == 1 and items[0]["type"] == "minecraft:item"
        assert items[0]["name"] == "Oak Log"
        assert "health" in items[0] and items[0]["health"] is None
        assert OBSERVER not in {item["name"] for item in players}
        assert BOT not in {item["name"] for item in players}

        missing = search.callable(
            {
                "entity_types": ["player"],
                "entity_name": "AbsentGuide",
                "search_radius": 16,
            }
        )
        after_missing = provider.body.get_state()
        assert not missing.success and missing.reason == "search_entity_not_found", missing.to_payload()
        assert distance(before.pos, after_missing.pos) < 0.25

        artifact = {
            "scope": "java_body_entity_read_and_canonical_search",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "body_provider": "java",
            "canonical_tool": "search_for_entity",
            "rcon_role": "fixture_setup_only",
            "found": {
                "reason": found.reason,
                "target": found.metrics["target"],
                "navigated": found.metrics["approach"]["navigated"],
                "wall_ms": round(search_wall_ms, 3),
            },
            "stable_guide_id": guide_first["id"],
            "cow": cows[0],
            "item": items[0],
            "observer_excluded": True,
            "not_found": {"reason": missing.reason, "can_retry": missing.can_retry},
            "movement": {
                "before": before.pos,
                "after_found": after_found.pos,
                "after_not_found": after_missing.pos,
            },
            "scarpet_body_constructed": False,
        }
        out = Path("logs/agentic-runtime/java-body-entity-read-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            command(rcon, "kill @e[tag=minebot.entity.probe]")
            command(rcon, "kill @e[type=minecraft:item]")
            for player in (GUIDE, OBSERVER, BOT):
                command(rcon, f"player {player} kill")
            wait_for_presence(provider.body, present=False)
        except Exception:
            pass
        provider.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
