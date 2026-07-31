#!/usr/bin/env python3
"""Live Java-only proof for round-trip collection and Body-owned ascent inventory."""

from __future__ import annotations

import json
import math
from pathlib import Path
import time

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaReturnProbe"
BODY_URL = "ws://127.0.0.1:8767"
NATURAL = Region("return-safe-fixture", (-256, -128, -256), (256, 320, 256))
ARTIFACT = Path("logs/agentic-runtime/java-body-return-safe-recovery-20260730.json")
Y = 180


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    result = rcon.command(value)
    if delay:
        time.sleep(delay)
    return result


def wait_for_player(rcon: RconClient) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "No entity was found" not in rcon.command(f"data get entity {BOT} Pos"):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{BOT} did not join the fixture")


def block_is(rcon: RconClient, pos: tuple[int, int, int], block: str) -> bool:
    x, y, z = pos
    return "passed" in rcon.command(f"execute if block {x} {y} {z} minecraft:{block}").lower()


def runtime_for(goal: str):
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=NATURAL,
        java_body_url=BODY_URL,
    )
    parts = build_phase1_agent_runtime(
        body=provider.body,
        goal_text=goal,
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
        ),
        agent_name="JavaReturnSafeProbe",
    )
    return provider, parts


def prepare_bot(rcon: RconClient) -> None:
    command(rcon, f"player {BOT} spawn")
    wait_for_player(rcon)
    command(rcon, f"gamemode spectator {BOT}")
    command(rcon, f"clear {BOT}")
    command(rcon, f"effect clear {BOT}")


def run_round_trip_collection(rcon: RconClient) -> dict[str, object]:
    x, z = 208, 188
    origin = (x + 0.5, float(Y), z + 0.5)
    unsafe_target = (x + 3, Y - 3, z)
    safe_target = (x - 4, Y, z)
    command(rcon, f"tp {BOT} {origin[0]} {Y + 4} {origin[2]}")
    time.sleep(0.5)
    command(rcon, f"fill {x - 8} {Y - 7} {z - 5} {x + 8} {Y + 5} {z + 5} air")
    command(rcon, f"fill {x - 1} {Y - 1} {z - 1} {x + 1} {Y - 1} {z + 1} stone")
    command(rcon, f"setblock {x + 2} {Y - 4} {z} stone")
    command(rcon, f"setblock {unsafe_target[0]} {unsafe_target[1] - 1} {unsafe_target[2]} stone")
    command(rcon, f"setblock {unsafe_target[0]} {unsafe_target[1]} {unsafe_target[2]} spruce_log")
    command(rcon, f"fill {x - 4} {Y - 1} {z} {x - 2} {Y - 1} {z} stone")
    command(rcon, f"setblock {safe_target[0]} {safe_target[1]} {safe_target[2]} spruce_log")
    command(rcon, f"item replace entity {BOT} hotbar.0 with iron_axe 1")
    command(rcon, f"tp {BOT} {origin[0]} {origin[1]} {origin[2]}")
    command(rcon, f"gamemode survival {BOT}")
    time.sleep(2.0)

    provider, parts = runtime_for("collect 1 log without entering a route that cannot be exited")
    body = provider.body
    equip = parts.registry.get("equip_item").callable(
        {"item": "iron_axe", "target": "mainhand"}
    )
    started = time.monotonic()
    result = parts.registry.get("collect_resource").callable(
        {
            "item": "logs",
            "count": 1,
            "constraints": {
                "radius": 16,
                "auto_prerequisites": False,
                "max_candidates": 8,
                "max_mutating_calls": 8,
                "max_wall_s": 90,
            },
        }
    )
    collected_at = body.get_state()
    retreat = parts.registry.get("move_to").callable(
        {"pos": [x, Y, z], "radius": 1, "timeout_s": 30}
    )
    final = body.get_state()
    elapsed = time.monotonic() - started
    body.interrupt("bounded_probe_cleanup")
    logs = sum(
        int(count)
        for item, count in (final.inventory_counts or {}).items()
        if str(item).removeprefix("minecraft:").endswith(("_log", "_stem"))
    )
    success = bool(
        result.success
        and equip.success
        and logs >= 1
        and block_is(rcon, unsafe_target, "spruce_log")
        and block_is(rcon, safe_target, "air")
        and retreat.success
        and math.dist(final.pos, origin) <= 2.0
        and provider.scarpet_body is None
    )
    return {
        "success": success,
        "elapsed_wall_s": round(elapsed, 3),
        "collect_result": result.to_payload(),
        "equip_result": equip.to_payload(),
        "collected_at": list(collected_at.pos),
        "retreat_result": retreat.to_payload(),
        "final_pos": list(final.pos),
        "unsafe_drop_target_intact": block_is(rcon, unsafe_target, "spruce_log"),
        "safe_route_target_collected": block_is(rcon, safe_target, "air"),
        "logs": logs,
        "scarpet_constructed": provider.scarpet_body is not None,
    }


def run_log_scaffold_ascent(rcon: RconClient) -> dict[str, object]:
    x, z = 236, 212
    origin = (x + 0.5, float(Y), z + 0.5)
    roof = (x, Y + 3, z)
    command(rcon, f"gamemode spectator {BOT}")
    command(rcon, f"tp {BOT} {origin[0]} {Y + 6} {origin[2]}")
    time.sleep(0.5)
    command(rcon, f"fill {x - 5} {Y - 3} {z - 5} {x + 5} {Y + 8} {z + 5} air")
    command(rcon, f"setblock {x} {Y - 1} {z} stone")
    command(rcon, f"setblock {roof[0]} {roof[1]} {roof[2]} stone")
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with stone_pickaxe 1")
    command(rcon, f"item replace entity {BOT} hotbar.1 with spruce_log 2")
    command(rcon, f"tp {BOT} {origin[0]} {origin[1]} {origin[2]}")
    command(rcon, f"gamemode survival {BOT}")
    time.sleep(1.0)

    provider, parts = runtime_for("return to the surface")
    body = provider.body
    equip = parts.registry.get("equip_item").callable(
        {"item": "stone_pickaxe", "target": "mainhand"}
    )
    started = time.monotonic()
    result = parts.registry.get("go_to_surface").callable({"timeout_s": 30})
    elapsed = time.monotonic() - started
    final = body.get_state()
    body.interrupt("bounded_probe_cleanup")
    broken = list((result.metrics or {}).get("broken") or [])
    placed = list((result.metrics or {}).get("placed") or [])
    success = bool(
        result.success
        and equip.success
        and result.reason == "surface_reached"
        and final.pos[1] >= Y + 1
        and any(
            entry.get("block_id") == "minecraft:stone"
            and entry.get("tool_id") == "minecraft:stone_pickaxe"
            for entry in broken
        )
        and any(entry.get("block_id") == "minecraft:spruce_log" for entry in placed)
        and provider.scarpet_body is None
    )
    return {
        "success": success,
        "elapsed_wall_s": round(elapsed, 3),
        "result": result.to_payload(),
        "equip_result": equip.to_payload(),
        "final_pos": list(final.pos),
        "roof_cleared": block_is(rcon, roof, "air"),
        "broken": broken,
        "placed": placed,
        "scarpet_constructed": provider.scarpet_body is not None,
    }


def main() -> int:
    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        prepare_bot(rcon)
        try:
            collection = run_round_trip_collection(rcon)
            ascent = run_log_scaffold_ascent(rcon)
        finally:
            command(rcon, f"player {BOT} kill", delay=0)
            command(rcon, "fill 200 173 180 244 188 220 air", delay=0)

    artifact = {
        "scope": "java_body_return_safe_recovery",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "rcon_role": "fixture_setup_and_verification_only",
        "collection": collection,
        "ascent": ascent,
        "success": bool(collection["success"] and ascent["success"]),
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if artifact["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
