"""Bounded Java Body recovery -> wood output probe. Not a formal gate.

The scenario creates a sealed underground stone corridor in the disposable
golden test world. Production code receives no fixture coordinates: the probe
invokes the canonical ``go_to_surface`` and ``collect_resource`` tools through
the composite provider and requires server-authoritative position/inventory
truth. Every stair break is answered by the real GovernancePolicy over a live
voxel re-read.
"""

from __future__ import annotations

import json
from pathlib import Path
import time

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Region, ToolResult
from minebot.game import RconClient, ScarpetBody
from minebot.game.rcon import RconConfig


BOT = "JavaAscendProbe"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("ascend-probe-natural", (-128, -64, -128), (128, 320, 128))
# Two stair steps east of this chamber emerge onto a bounded flat-land fixture.
# Production code receives none of these coordinates.
START_X = 98
START_Y = 98
START_Z = 100
SURFACE_X = 100
SURFACE_Y = 100
TREE_X = 106


def inventory_counts(body: ScarpetBody) -> dict[str, int]:
    counts: dict[str, int] = {}
    start: int | None = 0
    while start is not None:
        page = body.perceive("inventory", {"start": start, "limit": 12})
        if not page.ok:
            raise AssertionError(page.error)
        for slot in page.data.get("slots") or []:
            item = str(slot.get("item") or "").removeprefix("minecraft:")
            if item:
                counts[item] = counts.get(item, 0) + int(slot.get("count") or 0)
        cursor = page.data.get("nextStart") if page.data.get("nextStart") is not None else page.next
        start = int(cursor) if cursor is not None else None
    return counts


def prepare_corridor(rcon: RconClient) -> None:
    rcon.command(f"player {BOT} spawn")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "No entity was found" not in rcon.command(f"data get entity {BOT} Pos"):
            break
        time.sleep(0.25)
    else:
        raise RuntimeError(f"{BOT} did not join the test world")
    # Load the target chunk before issuing fixture mutations. A fresh golden
    # world may not have this area resident yet, and setblock then rejects the
    # write instead of generating/loading the chunk for us.
    rcon.command(f"gamemode spectator {BOT}")
    rcon.command(f"tp {BOT} {START_X + 0.5} {START_Y + 16} {START_Z + 0.5}")
    time.sleep(0.5)
    rcon.command(f"clear {BOT}")
    rcon.command("fill 94 96 94 114 98 106 dirt")
    rcon.command("fill 94 99 94 114 99 106 grass_block")
    rcon.command("fill 94 100 94 114 108 106 air")
    rock_palette = ("stone", "deepslate", "sandstone")
    for x in range(96, 100):
        for y in range(96, 100):
            for z in range(98, 103):
                block = rock_palette[(x + 2 * y + 3 * z) % len(rock_palette)]
                rcon.command(f"setblock {x} {y} {z} {block}")
    rcon.command(f"setblock {SURFACE_X} {SURFACE_Y - 1} {START_Z} grass_block")

    # A rooted tree gives production governance the same natural evidence as
    # the standalone golden-world collection proof.
    for y in range(100, 104):
        rcon.command(f"setblock {TREE_X} {y} {START_Z} oak_log")
    for x in range(TREE_X - 2, TREE_X + 3):
        for y in range(103, 106):
            for z in range(START_Z - 2, START_Z + 3):
                if x == TREE_X and z == START_Z and y <= 103:
                    continue
                if abs(x - TREE_X) + abs(z - START_Z) <= 3:
                    rcon.command(f"setblock {x} {y} {z} oak_leaves")

    # Recovery deliberately cannot harvest resource blocks. The chamber uses
    # only admitted non-resource rock, while step two lands on grass.
    rcon.command(f"setblock {START_X} {START_Y} {START_Z} air")
    rcon.command(f"setblock {START_X} {START_Y + 1} {START_Z} air")
    rcon.command(f"setblock {START_X} {START_Y + 2} {START_Z} sandstone")
    rcon.command(f"setblock {START_X + 1} {START_Y + 1} {START_Z} stone")
    rcon.command(f"setblock {START_X + 1} {START_Y + 2} {START_Z} deepslate")
    rcon.command(f"setblock {START_X + 1} {START_Y + 3} {START_Z} sandstone")
    rcon.command(f"setblock {START_X} {START_Y - 1} {START_Z} sandstone")
    rcon.command(f"gamemode survival {BOT}")
    rcon.command(f"give {BOT} iron_pickaxe 1")
    rcon.command(f"tp {BOT} {START_X + 0.5} {START_Y} {START_Z + 0.5}")
    # Let teleport physics settle onto the fixture floor before the Body reads
    # the starting foot cell.
    time.sleep(0.5)
    floor = rcon.command(
        f"execute if block {START_X} {START_Y - 1} {START_Z} minecraft:sandstone"
    )
    if "passed" not in floor.lower():
        raise RuntimeError(f"ascend fixture floor missing: {floor}")
    land_checks = {
        "surface_support": rcon.command(
            f"execute if block {SURFACE_X} {SURFACE_Y - 1} {START_Z} minecraft:grass_block"
        ),
        "surface_feet": rcon.command(
            f"execute if block {SURFACE_X} {SURFACE_Y} {START_Z} minecraft:air"
        ),
        "surface_head": rcon.command(
            f"execute if block {SURFACE_X} {SURFACE_Y + 1} {START_Z} minecraft:air"
        ),
        "nearby_tree": rcon.command(
            f"execute if block {TREE_X} {SURFACE_Y} {START_Z} minecraft:oak_log"
        ),
    }
    failed = {name: result for name, result in land_checks.items() if "passed" not in result.lower()}
    if failed:
        raise RuntimeError(f"land recovery fixture mismatch: {failed}")


def main() -> int:
    artifact: dict[str, object] = {
        "scope": "java_body_ascend_recovery_to_wood",
        "formal_gate": False,
        "bounded": True,
    }
    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        prepare_corridor(rcon)
        scarpet = ScarpetBody(BOT, rcon)
        provider = build_body_provider(
            "composite",
            bot_name=BOT,
            natural_region=REGION,
            scarpet_body=scarpet,
            java_body_url=BODY_URL,
        )
        parts = build_phase1_agent_runtime(
            body=provider.body,
            goal_text="collect 1 logs",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=REGION,
                body_provider="composite",
                governance_policy=provider.governance,
            ),
            agent_name="JavaAscendProbe",
        )
        try:
            before_state = provider.body.get_state()
            before_inventory = inventory_counts(scarpet)
            surface = parts.registry.get("go_to_surface").callable({"timeout_s": 120})
            after_surface = provider.body.get_state()
            before_wood = inventory_counts(scarpet)
            if surface.success:
                wood = parts.registry.get("collect_resource").callable(
                    {
                        "item": "logs",
                        "count": 1,
                        "constraints": {
                            "radius": 48,
                            "max_candidates": 8,
                            "max_mutating_calls": 8,
                            "max_wall_s": 120,
                        },
                    }
                )
            else:
                wood = ToolResult(False, "surface_prerequisite_failed", False)
            after_wood_state = provider.body.get_state()
            after_inventory = inventory_counts(scarpet)
            fixture_floor_intact = "passed" in rcon.command(
                f"execute if block {START_X} {START_Y - 1} {START_Z} minecraft:sandstone"
            ).lower()
        finally:
            provider.body.interrupt("bounded_probe_cleanup")

    wood_items = {
        item: count
        for item, count in after_inventory.items()
        if item.endswith("_log") or item.endswith("_stem")
    }
    before_wood_count = sum(
        count
        for item, count in before_wood.items()
        if item.endswith("_log") or item.endswith("_stem")
    )
    after_wood_count = sum(wood_items.values())
    artifact.update(
        {
            "start": {"pos": list(before_state.pos), "inventory": before_inventory},
            "surface_result": surface.to_payload(),
            "after_surface": {"pos": list(after_surface.pos), "missing": after_surface.missing},
            "wood_result": wood.to_payload(),
            "after_wood": {"pos": list(after_wood_state.pos), "missing": after_wood_state.missing},
            "after_inventory": after_inventory,
            "wood_items": wood_items,
            "wood_delta": after_wood_count - before_wood_count,
            "fixture_floor_intact": fixture_floor_intact,
        }
    )
    success = (
        surface.success
        and after_surface.pos[1] > before_state.pos[1]
        and wood.success
        and after_wood_count > before_wood_count
        and fixture_floor_intact
    )
    artifact["success"] = success
    out = Path("logs/agentic-runtime/java-body-ascend-recovery-20260727.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
