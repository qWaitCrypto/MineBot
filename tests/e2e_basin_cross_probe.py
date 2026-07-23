#!/usr/bin/env python3
"""Directed real-Body probe: does the batch mobility fix cross the golden basin?

Reproduces the ag-20260723-060009 stall: a Body at the frontier the model
reached (~(-40,63,-27)) must make real progress toward the nearest authoritative
log domain across the water/lava choke.  Drives the production explore_for
transaction (no model, no goal driver) with the fixed reflex-exact predicate and
the governed-mobility exploration escalation.

Pass  = authoritative body displacement toward the target beyond the pre-fix
        ~16-block partial wall, or explore returns found/reached.
Honest = a typed no_path/mobility_blocked with real partial progress recorded.
This is a directed probe; it is not the AG-FP30 gate.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.brain.lifecycle import LifecycleState  # noqa: E402
from minebot.brain.modes import ModeRuntime  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import WeldContext, execute_tool  # noqa: E402
from minebot.game import RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import spawn_or_fail  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE  # noqa: E402

BOT = "BasinCrossProbe"
START = (-40, 63, -27)


def command(rcon: RconClient, text: str, delay: float = 0.05) -> str:
    out = rcon.command(text)
    if delay:
        time.sleep(delay)
    return out


def main() -> int:
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
        spawn_or_fail(body, START)
        time.sleep(0.5)
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        # give it scaffold + a pickaxe so governed place/break can actually act
        command(rcon, f"give {BOT} minecraft:cobblestone 64")
        command(rcon, f"give {BOT} minecraft:stone_pickaxe 1")
        time.sleep(0.3)
        before = body.get_state()
        bx, by, bz = before.pos
        print(f"spawned at {(round(bx,1), round(by,1), round(bz,1))}")

        region = Region("basin", (-160, 0, -160), (160, 128, 160))
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=region))
        weld = WeldContext(body=body, authority=ProgressAuthority(), goal_text="reach wood")

        t = time.monotonic()
        try:
            payload = execute_tool(
                registry.get("explore_for"),
                {
                    "block_targets": ["oak_log", "birch_log", "spruce_log"],
                    "max_distance": 128,
                    "max_regions": 12,
                    "scan_radius": 16,
                },
                weld,
            )
        except (TimeoutError, OSError) as exc:
            payload = {"success": False, "reason": "transport_error", "metrics": {"error": str(exc)}}
        dt = time.monotonic() - t

        after = body.get_state()
        ax, ay, az = after.pos
        displacement = ((ax - bx) ** 2 + (az - bz) ** 2) ** 0.5
        metrics = payload.get("metrics") or {}
        print(f"explore_for -> success={payload.get('success')} reason={payload.get('reason')} elapsed={round(dt,1)}s")
        print(f"final pos {(round(ax,1), round(ay,1), round(az,1))} | horizontal displacement={round(displacement,1)}")
        print(f"blocks_found={len(metrics.get('blocks') or [])} distance_consumed={round(float(metrics.get('budget',{}).get('distance_consumed',0) if isinstance(metrics.get('budget'),dict) else 0),1)}")
        # show governed-mobility fallback traces if any
        for f in (metrics.get("candidate_failures") or [])[:4]:
            navs = f.get("navigation_attempts") or []
            modes = [n.get("mode") for n in navs if isinstance(n, dict) and n.get("mode")]
            print("  region", f.get("region"), "reason", f.get("reason"), "modes", modes)
        command(rcon, f"player {BOT} kill")
    print(f"\nVERDICT: {'CROSSED/PROGRESSED' if displacement > 18 or (metrics.get('blocks')) else 'STILL_WALLED (<=18 blocks, honest)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
