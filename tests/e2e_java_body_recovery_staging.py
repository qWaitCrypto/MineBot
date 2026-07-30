"""Bounded Java-only recovery staging before vertical escape. Not a formal gate.

RCON creates and inspects a disposable chamber. The canonical Java
``go_to_surface`` action starts above an unsafe support block, must move to a
nearby dry supported column, then complete the existing governed pillar escape.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import time

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Body, Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "JavaStageProbe"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("recovery-stage-natural", (64, -64, 64), (128, 320, 128))
X = 104
Y = 100
Z = 104
SCAFFOLD = "cobblestone"
PILLAR_STEPS = 6


def inventory_counts(body: Body) -> dict[str, int]:
    counts: dict[str, int] = {}
    start: int | None = 0
    while start is not None:
        page = body.perceive("inventory", {"start": start, "limit": 46})
        if not page.ok:
            raise AssertionError(page.error)
        for slot in page.data.get("slots") or []:
            item = str(slot.get("item") or "").removeprefix("minecraft:")
            if item:
                counts[item] = counts.get(item, 0) + int(slot.get("count") or 0)
        cursor = page.data.get("nextStart") if page.data.get("nextStart") is not None else page.next
        start = int(cursor) if cursor is not None else None
    return counts


def wait_for_player(rcon: RconClient) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "No entity was found" not in rcon.command(f"data get entity {BOT} Pos"):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{BOT} did not join the test world")


def prepare_chamber(rcon: RconClient) -> None:
    rcon.command(f"player {BOT} spawn")
    wait_for_player(rcon)
    rcon.command(f"gamemode spectator {BOT}")
    rcon.command(f"tp {BOT} {X + 0.5} {Y + PILLAR_STEPS + 8} {Z + 0.5}")
    time.sleep(0.5)
    rcon.command(f"clear {BOT}")
    rcon.command(f"fill {X - 4} {Y - 1} {Z - 4} {X + 4} {Y - 1} {Z + 4} stone")
    rcon.command(
        f"fill {X - 4} {Y} {Z - 4} "
        f"{X + 4} {Y + PILLAR_STEPS + 1} {Z + 4} stone hollow"
    )
    rcon.command(
        f"fill {X - 3} {Y} {Z - 3} "
        f"{X + 3} {Y + PILLAR_STEPS} {Z + 3} air"
    )
    rcon.command(f"setblock {X} {Y - 1} {Z} magma_block")
    rcon.command(f"gamemode survival {BOT}")
    rcon.command(f"give {BOT} {SCAFFOLD} {PILLAR_STEPS + 2}")
    rcon.command(f"give {BOT} iron_pickaxe 1")
    rcon.command(f"tp {BOT} {X + 0.5} {Y} {Z + 0.5}")
    time.sleep(0.5)


def block_is(rcon: RconClient, x: int, y: int, z: int, block: str) -> bool:
    result = rcon.command(f"execute if block {x} {y} {z} minecraft:{block}")
    return "passed" in result.lower()


def main() -> int:
    artifact: dict[str, object] = {
        "scope": "java_body_recovery_staging",
        "formal_gate": False,
        "bounded": True,
        "body_provider": "java",
        "scarpet_body_constructed": False,
        "rcon_role": "fixture_setup_and_verification_only",
    }
    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        prepare_chamber(rcon)
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
            inventory_before = inventory_counts(provider.body)
            started = time.monotonic()
            result = parts.registry.get("go_to_surface").callable({"timeout_s": 60})
            wall_s = time.monotonic() - started
            after = provider.body.get_state()
            inventory_after = inventory_counts(provider.body)
            metrics = result.metrics or {}
            placed = list(metrics.get("placed") or [])
            stage_column = (
                [int(placed[0]["x"]), int(placed[0]["z"])]
                if placed
                else None
            )
            pillar_blocks = (
                [
                    block_is(rcon, stage_column[0], Y + offset, stage_column[1], SCAFFOLD)
                    for offset in range(PILLAR_STEPS)
                ]
                if stage_column is not None
                else []
            )
            roof_open = (
                block_is(
                    rcon,
                    stage_column[0],
                    Y + PILLAR_STEPS + 1,
                    stage_column[1],
                    "air",
                )
                if stage_column is not None
                else False
            )
            source_hazard_preserved = block_is(rcon, X, Y - 1, Z, "magma_block")
        finally:
            provider.body.interrupt("bounded_probe_cleanup")
            rcon.command(f"player {BOT} kill")

    scaffold_before = inventory_before.get(SCAFFOLD, 0)
    scaffold_after = inventory_after.get(SCAFFOLD, 0)
    artifact.update(
        {
            "start_pos": list(before.pos),
            "final_pos": list(after.pos),
            "surface_result": result.to_payload(),
            "inventory_before": inventory_before,
            "inventory_after": inventory_after,
            "scaffold_delta": scaffold_after - scaffold_before,
            "stage_column": stage_column,
            "pillar_blocks": pillar_blocks,
            "roof_open": roof_open,
            "source_hazard_preserved": source_hazard_preserved,
            "wall_s": wall_s,
        }
    )
    metrics = result.metrics or {}
    moved_horizontally = math.hypot(
        after.pos[0] - before.pos[0],
        after.pos[2] - before.pos[2],
    )
    success = (
        result.success
        and bool(metrics.get("recovery_staging_attempted"))
        and bool(metrics.get("recovery_staging_used"))
        and int(metrics.get("recovery_staging_candidate_count") or 0) >= 1
        and int(metrics.get("pillar_steps") or 0) == PILLAR_STEPS
        and moved_horizontally >= 0.8
        and after.pos[1] >= before.pos[1] + PILLAR_STEPS
        and scaffold_after == scaffold_before - PILLAR_STEPS
        and len(pillar_blocks) == PILLAR_STEPS
        and all(pillar_blocks)
        and roof_open
        and source_hazard_preserved
    )
    artifact["success"] = success
    out = Path("logs/agentic-runtime/java-body-recovery-staging-20260730.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
