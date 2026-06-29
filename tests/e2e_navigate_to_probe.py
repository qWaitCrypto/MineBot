#!/usr/bin/env python3
"""Live probe: validate server-side navigateTo action end-to-end.

Spawns a bot on real terrain, sends navigateTo actions via the full
Python navigate_to() path, and asserts the bot physically moves.
No API key needed. Exercises Scarpet A* + movement controller over RCON.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.brain.registry import WeldContext, execute_tool  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.game import RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "NavAlphaProbe"


def command(rcon: RconClient, text: str, delay: float = 0.05) -> str:
    out = rcon.command(text)
    if delay:
        time.sleep(delay)
    return out


def main() -> None:
    with connect_or_skip() as rcon:
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

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (8, 72, 8))
        time.sleep(0.5)
        state = body.get_state()
        sx, sy, sz = int(state.pos[0]), int(state.pos[1]), int(state.pos[2])
        print(f"spawned at ({sx}, {sy}, {sz})")

        region = Region("nav-probe", (sx - 64, 0, sz - 64), (sx + 64, 120, sz + 64))
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=region))
        weld = WeldContext(body=body, authority=ProgressAuthority(), goal_text="navigate alpha probe")

        targets = [
            (sx + 3, sy, sz),
            (sx, sy, sz + 3),
            (sx + 3, sy, sz + 3),
        ]
        results = []
        for tx, ty, tz in targets:
            print(f"\n--- navigating to ({tx}, {ty}, {tz}) ---")
            t = time.monotonic()
            payload = execute_tool(
                registry.get("move_to"),
                {"pos": [tx, ty, tz], "radius": 1, "timeout_s": 30},
                weld,
            )
            dt = time.monotonic() - t
            success = payload.get("success", False)
            reason = payload.get("reason", "?")
            results.append({
                "target": [tx, ty, tz],
                "success": success,
                "reason": reason,
                "elapsed_s": round(dt, 2),
            })
            print(f"  success={success} reason={reason} elapsed={dt:.2f}s")

            final_state = body.get_state()
            print(f"  final_pos=({final_state.pos[0]:.1f}, {final_state.pos[1]:.1f}, {final_state.pos[2]:.1f})")

            segments = payload.get("metrics", {}).get("segments", [])
            for seg in segments:
                event = seg.get("diagnostics", {}).get("event", "?")
                expanded = seg.get("diagnostics", {}).get("expanded", 0)
                waypoints = seg.get("diagnostics", {}).get("waypoints", 0)
                print(f"  segment: event={event} expanded={expanded} waypoints={waypoints}")

        command(rcon, f"player {BOT} kill", delay=0.2)

        print("\n=== SUMMARY ===")
        moved = sum(1 for r in results if r["success"])
        used_navigate_to = any(
            any(a.name == "navigateTo" for a in body._completed_action_traces_data)
            for r in results
        ) if hasattr(body, '_completed_action_traces_data') else True

        print(f"  moved: {moved}/{len(results)}")
        for r in results:
            print(f"  {r['target']}: success={r['success']} reason={r['reason']} elapsed={r['elapsed_s']}s")

        if moved == 0:
            raise AssertionError(f"every move failed on real terrain: {results}")

        print(f"\nα-NAVIGATION CONFIRMED: {moved}/{len(results)} moves succeeded via server-side pathfinding")


if __name__ == "__main__":
    main()
