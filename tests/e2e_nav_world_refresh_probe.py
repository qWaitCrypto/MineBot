#!/usr/bin/env python3
"""Live nav probe: prove the GridWorld real-terrain refresh fixes move_to.

Drives move_to against the REAL generated terrain near spawn through the same
build_phase1_registry path the real agent uses, in the directions that
previously yielded on the flat placeholder world (-z and elevated targets).
No API key needed: this exercises only the Body navigation transaction.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.brain.registry import WeldContext, execute_tool  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.game import RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402


BOT = "NavRefreshProbe"


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

        body = ScarpetBody(BOT, rcon)
        # Spawn into real terrain. Use a real generated location, not a staged box.
        spawn_or_fail(body, (8, 72, 8))
        time.sleep(0.5)
        state = body.get_state()
        sx, sy, sz = int(state.pos[0]), int(state.pos[1]), int(state.pos[2])
        print(f"spawned at {(sx, sy, sz)}")

        region = Region("nav-probe", (sx - 64, 0, sz - 64), (sx + 64, 120, sz + 64))
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=region))
        weld = WeldContext(body=body, authority=ProgressAuthority(), goal_text="nav probe")

        # Targets that previously yielded on the flat placeholder world:
        # a -z move, a +z move, and a short diagonal — all near spawn so the
        # only question is "does the planner see real terrain", not "is it far".
        targets = [
            (sx, sy, sz - 3),
            (sx, sy, sz + 3),
            (sx + 3, sy, sz - 3),
        ]
        results = []
        for tx, ty, tz in targets:
            t = time.monotonic()
            payload = execute_tool(
                registry.get("move_to"),
                {"pos": [tx, ty, tz], "radius": 1, "timeout_s": 30},
                weld,
            )
            dt = time.monotonic() - t
            refreshed = _refresh_cells(payload)
            results.append(
                {
                    "target": [tx, ty, tz],
                    "success": payload.get("success"),
                    "reason": payload.get("reason"),
                    "elapsed_s": round(dt, 2),
                    "refreshed_cells": refreshed,
                }
            )
            print(results[-1])

        moved = sum(1 for r in results if r["success"])
        any_refresh = any((r["refreshed_cells"] or 0) > 0 for r in results)
        print({"moved": moved, "of": len(results), "any_world_refresh": any_refresh})
        if not any_refresh:
            raise AssertionError("no world refresh diagnostics observed -- planner still on placeholder grid")
        if moved == 0:
            raise AssertionError(f"every move yielded -- nav still broken on real terrain: {results}")


def _refresh_cells(payload: dict) -> int | None:
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(metrics, dict):
        return None
    executed = metrics.get("segments")
    if not isinstance(executed, list):
        return None
    total = 0
    seen = False
    for seg in executed:
        diag = seg.get("diagnostics") if isinstance(seg, dict) else None
        if isinstance(diag, dict) and "refreshed_cells" in diag:
            seen = True
            total += int(diag.get("refreshed_cells") or 0)
    return total if seen else None


if __name__ == "__main__":
    main()
