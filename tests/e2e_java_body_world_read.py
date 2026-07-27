#!/usr/bin/env python3
"""Bounded dry-land proof for the Java Body world-read family. Not a gate."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body.world_read import read_block_facts, read_surface_columns
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import JavaBodyClient, websocket_transport
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaWorldProbe"
BODY_URL = "ws://127.0.0.1:8767"
BASE = (20, 200, 20)


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    response = rcon.command(value)
    if delay:
        time.sleep(delay)
    return response


def wait_for_presence(body: JavaBody, *, present: bool, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if body.get_state().missing == (not present):
            return
        time.sleep(0.1)
    raise AssertionError(f"FakePlayer presence did not become {present}")


def despawn_if_present(rcon: RconClient, body: JavaBody) -> None:
    if not body.get_state().missing:
        command(rcon, f"player {BOT} kill")
        wait_for_presence(body, present=False)


def wait_for_overworld_position(body: JavaBody, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = body.get_state()
        if (
            state.dimension == "minecraft:overworld"
            and abs(state.pos[0] - BASE[0] - 0.5) < 1.0
            and abs(state.pos[2] - BASE[2] - 0.5) < 1.0
        ):
            return
        time.sleep(0.1)
    raise AssertionError("FakePlayer did not settle at the overworld probe position")


def read_pages(body: JavaBody, scope: str, params: dict) -> tuple[list[dict], list[dict]]:
    key = "blocks"
    start = 0
    items: list[dict] = []
    pages: list[dict] = []
    while True:
        page = body.perceive(scope, {**params, "start": start})
        assert page.ok, page
        items.extend(page.data.get(key) or [])
        pages.append({
            "start": page.data.get("start"),
            "count": page.data.get("count"),
            "complete": page.complete,
            "next": page.next,
        })
        if page.next is None:
            assert page.complete, page
            return items, pages
        next_start = int(page.next)
        assert next_start > start, page
        start = next_start


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    client = JavaBodyClient(BOT, websocket_transport(BODY_URL))
    body = JavaBody(client, BOT)
    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        despawn_if_present(rcon, body)
        command(rcon, "fill 18 200 18 22 203 22 air")
        command(rcon, "fill 18 199 18 22 199 22 stone")
        command(rcon, "setblock 21 200 20 oak_log[axis=x]")
        command(rcon, "setblock 20 200 21 stone_slab[type=bottom,waterlogged=false]")
        command(rcon, "setblock 19 200 20 leaf_litter")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(
            rcon,
            f"execute in minecraft:overworld run tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]}",
        )
        wait_for_overworld_position(body)

        log = body.perceive("blockAt", {"x": 21, "y": 200, "z": 20})
        replaceable = body.perceive("blockAt", {"x": 19, "y": 200, "z": 20})
        unloaded = body.perceive(
            "blockAt", {"x": 20_000_000, "y": 64, "z": 20_000_000}
        )
        assert log.ok and log.complete
        assert log.data["type"] == "minecraft:oak_log"
        assert log.data["state"] == "SOLID"
        assert log.data["properties"]["axis"] == "x"
        assert replaceable.data["type"] == "minecraft:leaf_litter"
        assert replaceable.data["state"] == "CLEAR"
        assert unloaded.data["type"] == "unknown"
        assert unloaded.data["state"] == "UNLOADED"

        positions = ((20, 199, 20), (20, 200, 20), (21, 200, 20), (20, 200, 21))
        batch = read_block_facts(body, positions, page_size=2)
        assert batch[(20, 199, 20)].data["type"] == "minecraft:stone"
        assert batch[(20, 200, 20)].data["state"] == "CLEAR"
        assert batch[(20, 200, 21)].data["properties"]["type"] == "bottom"

        surfaces = read_surface_columns(body, ((20, 20),), page_size=1)
        surface = surfaces[(20, 20)]
        assert surface.feet_y == 200
        assert surface.feet_state == "CLEAR"
        assert surface.support_type == "minecraft:stone"
        assert surface.support_state == "SOLID"

        nearby, nearby_pages = read_pages(
            body, "nearbyBlocks", {"radius": 1, "limit": 4}
        )
        debug, debug_pages = read_pages(
            body, "debugBlocks", {"radius": 1, "limit": 4}
        )
        broad_started = time.monotonic()
        broad = body.perceive("nearbyBlocks", {"radius": 8, "limit": 256})
        broad_wall_ms = (time.monotonic() - broad_started) * 1000.0
        assert broad.ok and broad.complete
        assert any(item["type"] == "minecraft:oak_log" for item in nearby)
        assert not any(item["type"] == "minecraft:leaf_litter" for item in nearby)
        assert len(debug) == 27
        first_debug = body.perceive("debugBlocks", {"radius": 1, "limit": 4})
        assert not first_debug.complete and first_debug.next is not None
        assert first_debug.data["cursor"]["state"] == "CLEAR"
        assert first_debug.data["feet"]["type"] == "minecraft:stone"
        assert "Test passed" in command(
            rcon, "execute if block 21 200 20 minecraft:oak_log[axis=x]"
        )

        command(rcon, f"player {BOT} kill")
        wait_for_presence(body, present=False)
        missing = body.perceive("blockAt", {"x": 21, "y": 200, "z": 20})
        assert not missing.ok and missing.complete
        assert missing.error == "missing_body"

        artifact = {
            "scope": "java_body_world_read_family",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "facts": {
                "exact_log": log.data,
                "replaceable_leaf_litter_state": replaceable.data["state"],
                "unloaded_state": unloaded.data["state"],
                "batch_count": len(batch),
                "surface": {
                    "feet_y": surface.feet_y,
                    "feet_state": surface.feet_state,
                    "support_type": surface.support_type,
                },
                "nearby_count": len(nearby),
                "nearby_pages": nearby_pages,
                "debug_count": len(debug),
                "debug_pages": debug_pages,
                "radius_8": {
                    "count": broad.data["count"],
                    "server_cost_micros": broad.data["serverCostMicros"],
                    "wall_ms": round(broad_wall_ms, 3),
                },
                "rcon_cross_check": True,
            },
            "missing_body": {
                "ok": missing.ok,
                "complete": missing.complete,
                "error": missing.error,
            },
        }
        out = Path("logs/agentic-runtime/java-body-world-read-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            despawn_if_present(rcon, body)
        except Exception:
            pass
        client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
