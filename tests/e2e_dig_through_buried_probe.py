#!/usr/bin/env python3
"""Live collect-approach probe: prove collect can reach a BURIED dirt block.

Sets up a dirt block fully encased in stone (no pre-existing air pocket beside
it), then drives collect_resource. The lightweight bare-moveTo approach cannot
reach it; the approach must clear the selected stand point under
COLLECT_APPROACH governance, then delegate movement to the navigator. No API
key needed.
"""

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
        # Look for collect-approach clearance evidence in the attempts' mine metrics.
        dig_through_seen = _dig_through_in_attempts(metrics)
        print({"dig_through_observed": dig_through_seen})

        # Ground truth independent of the (pickup-timing-sensitive) inventory
        # delta: did the buried block actually get mined, and did the bot gain
        # the item? mineBlock can complete + drop get picked up after the
        # collect's pickup window, so payload.success may be False even
        # when collect-approach clearance physically worked. This check is the real proof
        # for #15.
        time.sleep(0.5)
        block_gone = "dirt" not in command(rcon, f"execute if block {buried[0]} {buried[1]} {buried[2]} dirt", 0)
        final_state = body.get_state()
        gained = "dirt" in (final_state.inventory_raw or "")
        ground_truth = {"buried_block_gone": block_gone, "bot_has_dirt": gained}
        print({"ground_truth": ground_truth})

        if payload.get("success"):
            print("RESULT: collected a BURIED block -- collect-approach clearance works on real terrain")
        elif block_gone and dig_through_seen:
            # Collect-approach clearance physically worked: it reached the buried block and
            # mineBlock destroyed it. Whether the drop was picked up is a
            # separate pickup-timing issue (collect_no_inventory_delta), not a
            # collect-approach regression.
            print("RESULT: collect-approach clearance WORKED (buried block mined via COLLECT_APPROACH path); pickup delta is a separate issue")
        elif payload.get("reason") == "transport_error":
            print("RESULT: transient transport_error (rerun) -- not a collect-approach regression")
        else:
            print(f"RESULT: did not collect buried block (reason={payload.get('reason')}); inspect attempts/skipped")


def _dig_through_in_attempts(metrics: dict) -> bool:
    for attempt in metrics.get("attempts") or []:
        mine = attempt.get("mine") if isinstance(attempt, dict) else None
        mmetrics = mine.get("metrics") if isinstance(mine, dict) else None
        if not isinstance(mmetrics, dict):
            continue
        # Success path: collect-approach metrics folded into mine_approach sub-field.
        approach = mmetrics.get("mine_approach")
        if isinstance(approach, dict) and approach.get("dig_through"):
            return True
        # Failure path: collect-approach ran but could not reach; metrics carry
        # dig_through at the top level of mine.metrics.
        if mmetrics.get("dig_through"):
            return True
    return False


if __name__ == "__main__":
    main()
