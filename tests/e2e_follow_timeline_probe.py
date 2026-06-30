#!/usr/bin/env python3
"""Timeline probe (single RCON client): prove follow tracks a mid-pursuit tp.

One RCON client only — the RconClient lock serializes the follow thread's event
polling against the main thread's tp + position sampling, so there is no
two-client server-side desync. Samples bot + target positions every 0.5s and
relocates the target at 2.5s, printing the timeline so the relocation and the
bot's re-path are visible.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.brain.registry import WeldContext, execute_tool  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.game import Region, ScarpetBody  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "FollowTL"
TARGET = "FollowTLT"


def _pos(body: ScarpetBody) -> tuple[float, float, float]:
    p = body.get_state().pos
    return (round(p[0], 2), round(p[1], 2), round(p[2], 2))


def _dist(a, b) -> float:
    return round(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5, 2)


def main():
    with connect_or_skip() as rcon:
        for cmd in [
            "script unload minebot", "script load minebot global",
            "carpet commandPlayer true", "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false", "time set day", "weather clear",
            f"player {BOT} kill", f"player {TARGET} kill",
            "fill 14 69 14 32 76 32 air",
            "fill 14 69 14 32 69 32 stone",
        ]:
            rcon.command(cmd)
            time.sleep(0.05)

        body = ScarpetBody(BOT, rcon)
        target_body = ScarpetBody(TARGET, rcon)
        spawn_or_fail(body, (20, 70, 20))
        spawn_or_fail(target_body, (28, 70, 20))
        time.sleep(0.5)

        region = Region("follow-tl", (12, 0, 12), (34, 120, 34))
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=region))
        weld = WeldContext(body=body, authority=ProgressAuthority(), goal_text="follow timeline")

        result = {}
        def follow():
            try:
                result["payload"] = execute_tool(
                    registry.get("follow_entity"),
                    {"target": TARGET, "keep_distance": 2.0, "timeout_s": 9.0},
                    weld,
                )
            except Exception as exc:
                result["error"] = repr(exc)

        th = threading.Thread(target=follow)
        th.start()

        t0 = time.monotonic()
        tp_done = False
        rows = []
        while time.monotonic() - t0 < 9.5:
            if not tp_done and time.monotonic() - t0 >= 2.5:
                rcon.command(f"tp {TARGET} 20 70 28")
                tp_done = True
                rows.append((round(time.monotonic() - t0, 2), "TP_ISSUED -> 20 70 28", None, None, None))
            b = _pos(body)
            tgt = _pos(target_body)
            rows.append((round(time.monotonic() - t0, 2), "", b, tgt, _dist(b, tgt)))
            time.sleep(0.5)
        th.join(timeout=12.0)
        rcon.command(f"player {BOT} kill")
        time.sleep(0.2)
        rcon.command(f"player {TARGET} kill")
        time.sleep(0.2)

        print("time(s)  bot                               target                            dist")
        for t, ev, b, tgt, d in rows:
            if ev:
                print(f"{t:6.2f}   {ev}")
            else:
                print(f"{t:6.2f}   bot={b}   target={tgt}   dist={d}")
        print("reason:", result.get("payload", {}).get("reason") if result.get("payload") else result.get("error"))

        final = next((r for r in reversed(rows) if r[4] is not None), None)
        if final and final[4] > 4.5:
            raise AssertionError(f"bot did not end near target: dist={final[4]}")


if __name__ == "__main__":
    main()
