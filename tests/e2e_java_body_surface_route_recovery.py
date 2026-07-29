"""Bounded Java-only walkable surface recovery. Not a formal gate.

RCON creates and inspects a disposable covered tunnel. Production receives no
fixture coordinates: the canonical ``go_to_surface`` intent must walk through
the existing exit without breaking or placing blocks.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import time

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "JavaSurfProbe"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("surface-route-probe-natural", (64, -64, 64), (112, 320, 112))
X = 80
Y = 100
Z = 80


def wait_for_player(rcon: RconClient) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "No entity was found" not in rcon.command(f"data get entity {BOT} Pos"):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{BOT} did not join the test world")


def prepare_tunnel(rcon: RconClient) -> None:
    rcon.command(f"player {BOT} spawn")
    wait_for_player(rcon)
    rcon.command(f"gamemode spectator {BOT}")
    rcon.command(f"tp {BOT} {X + 0.5} {Y + 16} {Z + 0.5}")
    time.sleep(0.5)
    rcon.command(f"clear {BOT}")
    rcon.command(f"fill {X - 8} {Y - 1} {Z - 8} {X + 16} {Y - 1} {Z + 8} stone")
    rcon.command(f"fill {X - 8} {Y} {Z - 8} {X + 16} {Y + 8} {Z + 8} air")
    rcon.command(f"fill {X - 2} {Y} {Z - 2} {X + 3} {Y + 2} {Z + 2} stone hollow")
    rcon.command(f"fill {X - 1} {Y} {Z - 1} {X + 4} {Y + 1} {Z + 1} air")
    rcon.command(f"gamemode survival {BOT}")
    rcon.command(f"tp {BOT} {X + 0.5} {Y} {Z + 0.5}")
    time.sleep(0.5)


def main() -> int:
    artifact: dict[str, object] = {
        "scope": "java_body_walkable_surface_recovery",
        "formal_gate": False,
        "bounded": True,
        "body_provider": "java",
        "scarpet_body_constructed": False,
        "rcon_role": "fixture_setup_and_verification_only",
    }
    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        prepare_tunnel(rcon)
        provider = build_body_provider(
            "java",
            bot_name=BOT,
            natural_region=REGION,
            java_body_url=BODY_URL,
        )
        parts = build_phase1_agent_runtime(
            body=provider.body,
            goal_text="return to the surface",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=REGION,
                body_provider="java",
                governance_policy=provider.governance,
            ),
            agent_name=BOT,
        )
        try:
            before = provider.body.get_state()
            started = time.monotonic()
            result = parts.registry.get("go_to_surface").callable({"timeout_s": 30})
            wall_s = time.monotonic() - started
            after = provider.body.get_state()
        finally:
            provider.body.interrupt("bounded_probe_cleanup")

    metrics = result.metrics or {}
    broken = list(metrics.get("broken") or [])
    placed = list(metrics.get("placed") or [])
    artifact.update(
        {
            "start_pos": list(before.pos),
            "final_pos": list(after.pos),
            "surface_result": result.to_payload(),
            "wall_s": wall_s,
        }
    )
    success = (
        result.success
        and bool(metrics.get("surface_route_used"))
        and math.hypot(after.pos[0] - before.pos[0], after.pos[2] - before.pos[2]) >= 3.0
        and int(metrics.get("ascend_steps") or 0) == 0
        and not broken
        and not placed
    )
    artifact["success"] = success
    out = Path("logs/agentic-runtime/java-body-surface-route-recovery-20260729.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
