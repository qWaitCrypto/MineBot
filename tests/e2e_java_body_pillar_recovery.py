"""Bounded Java-only physical pillar escape. Not a formal gate.

RCON creates and inspects the disposable fixture only. The production action
enters through canonical ``go_to_surface`` with a Java provider and no
ScarpetBody. Success requires real height, block, and inventory deltas.
"""

from __future__ import annotations

import json
from pathlib import Path
import time

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.contract import Body, Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "JavaPillarProbe"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("pillar-probe-natural", (-128, -64, -128), (128, 320, 128))
X = 120
Y = 100
Z = 120
SCAFFOLD = "cobblestone"


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


def prepare_shaft(rcon: RconClient) -> None:
    rcon.command(f"player {BOT} spawn")
    wait_for_player(rcon)
    rcon.command(f"gamemode spectator {BOT}")
    rcon.command(f"tp {BOT} {X + 0.5} {Y + 16} {Z + 0.5}")
    time.sleep(0.5)
    rcon.command(f"clear {BOT}")

    # A sealed 7x7 chamber keeps every scanned surface physically unreachable.
    # The open interior removes adjacent stair support, so the player must
    # pillar twice and break the roof above its own column.
    rcon.command(f"fill {X - 3} {Y - 1} {Z - 3} {X + 3} {Y - 1} {Z + 3} stone")
    rcon.command(f"fill {X - 3} {Y} {Z - 3} {X + 3} {Y + 3} {Z + 3} stone hollow")
    rcon.command(f"fill {X - 2} {Y} {Z - 2} {X + 2} {Y + 2} {Z + 2} air")
    rcon.command(f"gamemode survival {BOT}")
    rcon.command(f"give {BOT} {SCAFFOLD} 8")
    rcon.command(f"give {BOT} iron_pickaxe 1")
    rcon.command(f"tp {BOT} {X + 0.5} {Y} {Z + 0.5}")
    time.sleep(0.5)


def block_is(rcon: RconClient, y: int, block: str) -> bool:
    result = rcon.command(f"execute if block {X} {y} {Z} minecraft:{block}")
    return "passed" in result.lower()


def main() -> int:
    artifact: dict[str, object] = {
        "scope": "java_body_pillar_recovery",
        "formal_gate": False,
        "bounded": True,
        "body_provider": "java",
        "scarpet_body_constructed": False,
        "rcon_role": "fixture_setup_and_verification_only",
    }
    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        prepare_shaft(rcon)
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
            first_pillar = block_is(rcon, Y, SCAFFOLD)
            second_pillar = block_is(rcon, Y + 1, SCAFFOLD)
            roof_open = block_is(rcon, Y + 3, "air")
        finally:
            provider.body.interrupt("bounded_probe_cleanup")

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
            "first_pillar": first_pillar,
            "second_pillar": second_pillar,
            "roof_open": roof_open,
            "wall_s": wall_s,
        }
    )
    metrics = result.metrics or {}
    success = (
        result.success
        and after.pos[1] >= before.pos[1] + 2.0
        and int(metrics.get("pillar_steps") or 0) >= 2
        and scaffold_after == scaffold_before - 2
        and first_pillar
        and second_pillar
        and roof_open
    )
    artifact["success"] = success
    out = Path("logs/agentic-runtime/java-body-pillar-recovery-20260729.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
