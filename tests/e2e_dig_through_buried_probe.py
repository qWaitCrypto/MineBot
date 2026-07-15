#!/usr/bin/env python3
"""Live gate: one goal-set navigation process collects a buried dirt block."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.brain.composition import (  # noqa: E402
    CompositionBudget,
    CompositionContext,
    register_collect_resource_tool,
)
from minebot.brain.lifecycle import LifecycleState  # noqa: E402
from minebot.brain.modes import ModeRuntime  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import WeldContext, execute_tool  # noqa: E402
from minebot.game import RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402


BOT = "DigThroughProbe"


def command(rcon: RconClient, text: str, delay: float = 0.05) -> str:
    out = rcon.command(text)
    if delay:
        time.sleep(delay)
    return out


def main() -> None:
    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        for cmd in [
            "script unload minebot",
            "script load minebot global",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false",
            "time set day",
            "weather clear",
            f"player {BOT} kill",
        ]:
            command(rcon, cmd)

        # Build a controlled arena: stone platform with a dirt block buried one
        # layer under the surface, fully encased (no adjacent air pocket).
        ox, oy, oz = 100, 72, 100
        command(rcon, f"fill {ox-6} {oy-2} {oz-6} {ox+6} {oy+4} {oz+6} air")
        command(rcon, f"fill {ox-6} {oy-3} {oz-6} {ox+6} {oy-1} {oz+6} stone")  # 3-thick stone floor
        # Bury a dirt block in the middle of the stone (one below the top floor).
        buried = (ox + 3, oy - 2, oz)
        command(rcon, f"setblock {buried[0]} {buried[1]} {buried[2]} dirt")

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (ox, oy, oz))
        time.sleep(0.5)
        command(rcon, f"clear {BOT}")
        command(rcon, f"gamemode survival {BOT}")
        time.sleep(0.3)
        state = body.get_state()
        sx, sy, sz = int(state.pos[0]), int(state.pos[1]), int(state.pos[2])
        print(f"spawned at {(sx, sy, sz)}; buried dirt at {buried}")

        region = Region("dig-probe", (ox - 32, 0, oz - 32), (ox + 32, 120, oz + 32))
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=region))
        weld = WeldContext(body=body, authority=ProgressAuthority(), goal_text="collect 1 dirt (buried)")
        ctx = CompositionContext(
            registry=registry,
            weld_context=weld,
            runtime_profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            budget=CompositionBudget(max_candidates=6, max_mutating_calls=12, max_wall_s=90.0),
        )
        register_collect_resource_tool(registry, ctx)

        t = time.monotonic()
        try:
            payload = execute_tool(
                registry.get("collect_resource"),
                {"item": "dirt", "count": 1, "constraints": {"radius": 12, "max_candidates": 6}},
                weld,
            )
        except (TimeoutError, OSError) as exc:
            payload = {"success": False, "reason": "transport_error", "metrics": {"error": str(exc)}}
        dt = time.monotonic() - t

        metrics = payload.get("metrics") or {}
        summary = {
            "success": payload.get("success"),
            "reason": payload.get("reason"),
            "after_count": metrics.get("after_count"),
            "candidates_tried": metrics.get("candidates_tried"),
            "elapsed_s": round(dt, 1),
        }
        print(summary)
        navigation_evidence = _goal_set_navigation_evidence(metrics)
        print({"goal_set_navigation": navigation_evidence})

        time.sleep(0.5)
        buried_fact = body.perceive(
            "blockAt",
            {"x": buried[0], "y": buried[1], "z": buried[2]},
        )
        block_gone = bool(
            buried_fact.ok
            and buried_fact.complete
            and str(buried_fact.data.get("state") or "").upper() == "CLEAR"
        )
        dirt_count = sum(
            int(slot.count)
            for slot in body.get_inventory()
            if str(slot.item or "").removeprefix("minecraft:") == "dirt"
        )
        gained = dirt_count >= 1
        ground_truth = {
            "buried_block_gone": block_gone,
            "buried_block": dict(buried_fact.data),
            "bot_has_dirt": gained,
            "dirt_count": dirt_count,
        }
        print({"ground_truth": ground_truth})

        if not payload.get("success") or payload.get("reason") != "collected":
            raise AssertionError(f"buried collect did not complete: {payload}")
        if int(metrics.get("after_count") or 0) < 1 or not gained:
            raise AssertionError(f"buried collect lacked inventory truth: payload={payload} ground_truth={ground_truth}")
        if not block_gone:
            raise AssertionError(f"buried target remained in world: {ground_truth}")
        if not navigation_evidence["goal_set_preserved"] or not navigation_evidence["governed_terrain_step"]:
            raise AssertionError(f"buried collect bypassed unified terrain navigation: {navigation_evidence}")
        print("RESULT: buried collect used one governed goal-set navigation process and gained the item")


def _goal_set_navigation_evidence(metrics: dict) -> dict[str, object]:
    goal_set_preserved = False
    governed_terrain_step = False
    movement_counts: dict[str, int] = {}

    def visit(value: object) -> None:
        nonlocal goal_set_preserved, governed_terrain_step
        if isinstance(value, dict):
            goal = value.get("navigation_goal")
            if isinstance(goal, dict) and goal.get("kind") == "composite" and goal.get("mode") == "any":
                goal_set_preserved = True
            counts = value.get("movement_counts")
            if isinstance(counts, dict):
                for kind, count in counts.items():
                    movement_counts[str(kind)] = max(movement_counts.get(str(kind), 0), int(count or 0))
                if int(counts.get("break") or 0) > 0 or int(counts.get("downward") or 0) > 0:
                    governed_terrain_step = True
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(metrics)
    return {
        "goal_set_preserved": goal_set_preserved,
        "governed_terrain_step": governed_terrain_step,
        "movement_counts": movement_counts,
    }


if __name__ == "__main__":
    main()
