#!/usr/bin/env python3
"""Live probe: validate server-side followEntity end-to-end.

Spawns a bot and a target fake player. The bot follows the target by name;
the probe tp's the target mid-follow and asserts the bot tracks the move and
holds within keep_distance. No API key. Exercises Scarpet followEntity +
run_follow_tick + run_move_tick (the "navigateTo with a moving goal" path).
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
from minebot.game import RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "FollowProbe"
TARGET = "FollowTarget"


def command(rcon, text, delay=0.05):
    out = rcon.command(text)
    if delay:
        time.sleep(delay)
    return out


def main():
    with connect_or_skip() as rcon:
        for cmd in [
            "script unload minebot", "script load minebot global",
            "carpet commandPlayer true", "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false", "time set day", "weather clear",
            f"player {BOT} kill", f"player {TARGET} kill",
            "fill 20 70 20 28 76 28 air",
            "fill 20 69 20 28 69 28 stone",
        ]:
            command(rcon, cmd)

        body = ScarpetBody(BOT, rcon)
        target_body = ScarpetBody(TARGET, rcon)
        spawn_or_fail(body, (20, 70, 20))
        spawn_or_fail(target_body, (24, 70, 24))
        time.sleep(0.5)

        region = Region("follow-probe", (16, 0, 16), (32, 120, 32))
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=region))
        weld = WeldContext(body=body, authority=ProgressAuthority(), goal_text="follow probe")

        # Phase 1: follow a static target, expect the bot to close to keep_distance.
        follow_result = {}
        def follow_static():
            t0 = time.monotonic()
            payload = execute_tool(
                registry.get("follow_entity"),
                {"target": TARGET, "keep_distance": 2.0, "timeout_s": 6.0},
                weld,
            )
            follow_result["payload"] = payload
            follow_result["elapsed"] = time.monotonic() - t0

        th = threading.Thread(target=follow_static)
        th.start()
        th.join(timeout=12.0)
        if th.is_alive():
            command(rcon, f"player {BOT} stop")
            raise AssertionError("follow_entity did not return within 12s (expected timeout terminal)")

        payload = follow_result["payload"]
        bot_state = body.get_state()
        tgt_state = target_body.get_state()
        dist_after = ((bot_state.pos[0] - tgt_state.pos[0]) ** 2 +
                      (bot_state.pos[1] - tgt_state.pos[1]) ** 2 +
                      (bot_state.pos[2] - tgt_state.pos[2]) ** 2) ** 0.5
        print(f"phase1 (static target): reason={payload.get('reason')} dist_after={dist_after:.2f} elapsed={follow_result['elapsed']:.2f}s")
        print(f"  bot={bot_state.pos} target={tgt_state.pos}")

        if payload.get("success") is not True or payload.get("reason") != "arrived":
            raise AssertionError(f"follow_entity returned non-success payload for static target: {payload}")
        if dist_after > 4.0:
            raise AssertionError(f"bot did not close to target within keep_distance+tolerance: dist={dist_after:.2f}")

        # Phase 2: tp the target away mid-follow, verify the bot tracks the move.
        command(rcon, f"tp {TARGET} 28 70 20")
        time.sleep(0.3)

        follow_result2 = {}
        def follow_moved():
            t0 = time.monotonic()
            payload = execute_tool(
                registry.get("follow_entity"),
                {"target": TARGET, "keep_distance": 2.0, "timeout_s": 8.0},
                weld,
            )
            follow_result2["payload"] = payload
            follow_result2["elapsed"] = time.monotonic() - t0

        th2 = threading.Thread(target=follow_moved)
        th2.start()
        # While following, move the target again so the bot must re-plan mid-pursuit.
        time.sleep(2.5)
        command(rcon, f"tp {TARGET} 20 70 28")
        th2.join(timeout=14.0)
        if th2.is_alive():
            command(rcon, f"player {BOT} stop")
            raise AssertionError("follow_entity (moved) did not return within 14s")

        payload2 = follow_result2["payload"]
        bot_state2 = body.get_state()
        tgt_state2 = target_body.get_state()
        dist_after2 = ((bot_state2.pos[0] - tgt_state2.pos[0]) ** 2 +
                       (bot_state2.pos[1] - tgt_state2.pos[1]) ** 2 +
                       (bot_state2.pos[2] - tgt_state2.pos[2]) ** 2) ** 0.5
        print(f"phase2 (moved target): reason={payload2.get('reason')} dist_after={dist_after2:.2f} elapsed={follow_result2['elapsed']:.2f}s")
        print(f"  bot={bot_state2.pos} target={tgt_state2.pos}")

        command(rcon, f"player {BOT} kill", delay=0.2)
        command(rcon, f"player {TARGET} kill", delay=0.2)

        if payload2.get("success") is not True or payload2.get("reason") != "arrived":
            raise AssertionError(f"follow_entity returned non-success payload for moved target: {payload2}")
        if dist_after2 > 4.5:
            raise AssertionError(f"bot did not track the moved target: dist={dist_after2:.2f}")

        print("\nFOLLOW CONFIRMED: bot closed to a static target AND tracked a mid-pursuit tp, holding within keep_distance each time.")


if __name__ == "__main__":
    main()
