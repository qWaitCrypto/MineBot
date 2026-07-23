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

import argparse
import json
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
ROOT = Path(__file__).resolve().parents[1]


def command(rcon: RconClient, text: str, delay: float = 0.05) -> str:
    out = rcon.command(text)
    if delay:
        time.sleep(delay)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

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
            "kill @e[type=minecraft:item]",
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
        candidate_summaries = []
        for f in (metrics.get("candidate_failures") or [])[:8]:
            if not isinstance(f, dict):
                continue
            navs = f.get("navigation_attempts") or []
            modes = [n.get("mode") for n in navs if isinstance(n, dict) and n.get("mode")]
            candidate_summaries.append(
                {
                    "region": f.get("region"),
                    "reason": f.get("reason"),
                    "modes": modes,
                    "attempt_reasons": [
                        n.get("reason") for n in navs if isinstance(n, dict)
                    ],
                }
            )
        progressed = displacement > 18 or bool(metrics.get("blocks"))
        report = {
            "schema_version": 1,
            "scope": "Q1_hard_basin_cross_probe",
            "bounded": True,
            "formal_gate": False,
            "bot": BOT,
            "world_fixture": "world-golden",
            "start": list(START),
            "actual_start_pos": [round(bx, 3), round(by, 3), round(bz, 3)],
            "final_pos": [round(ax, 3), round(ay, 3), round(az, 3)],
            "horizontal_displacement": round(displacement, 3),
            "elapsed_s": round(dt, 3),
            "tool": "explore_for",
            "tool_result": {
                "success": payload.get("success"),
                "reason": payload.get("reason"),
                "canRetry": payload.get("canRetry"),
            },
            "budget": metrics.get("budget") if isinstance(metrics.get("budget"), dict) else {},
            "blocks_found": len(metrics.get("blocks") or []),
            "candidate_failures": candidate_summaries,
            "classification": {
                "verdict": "pass" if progressed else "fail",
                "reason": "crossed_or_found_target" if progressed else "still_walled",
            },
            "evidence_limits": [
                "This is a directed Body mechanism probe, not an AG-FP30/Q4/Q5 gate.",
                "It starts at the historical basin/frontier and gives scaffold/pickaxe to isolate mobility capability, so it is supporting evidence rather than production ingress proof.",
                "STILL_WALLED is a typed Q1 blocker and must not be counted as a pass.",
            ],
        }
        output = args.output or ROOT / "logs" / "agentic-runtime" / f"q1-basin-cross-{int(time.time())}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        command(rcon, f"clear {BOT}", delay=0.05)
        command(rcon, f"player {BOT} kill")
        command(rcon, "kill @e[type=minecraft:item]", delay=0.05)
    return 0 if report["classification"]["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
