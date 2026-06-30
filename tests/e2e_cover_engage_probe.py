#!/usr/bin/env python3
"""Live probe: validate cover-aware A* for a ranged target (S4).

A NoAI skeleton (ranged) stands at (28,70,20). A 2-high wall at z=21, x=22-27
blocks line-of-sight from a z=22 approach lane to the skeleton, but does NOT
block the direct z=20 path. So:
  - direct path (z=20): clear but EXPOSED (skeleton has LOS along z=20).
  - cover lane  (z=22): clear and COVERED (wall breaks LOS to the skeleton).
With cover-aware A* (engage_replan passes cover_target for ranged targets), the
bot should prefer the covered z=22 lane. The probe samples the bot's z over time
and asserts it reached the cover lane (max_z >= 21) AND killed the skeleton.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.contract import Action  # noqa: E402
from minebot.game import ScarpetBody  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402

BOT = "CoverProbe"


def main():
    with connect_or_skip() as rcon:
        for cmd in [
            "script unload minebot", "script load minebot global",
            "carpet commandPlayer true", "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false", "time set 18000", "weather clear",
            f"player {BOT} kill", "kill @e[type=skeleton]",
            "fill 20 69 20 28 76 28 air",
            "fill 20 69 20 28 69 28 stone",
            "fill 22 70 21 27 71 21 stone",
        ]:
            rcon.command(cmd)
            time.sleep(0.05)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (20, 70, 20))
        rcon.command("summon skeleton 28 70 20 {NoAI:1b,PersistenceRequired:1b,Health:20f}")
        time.sleep(0.5)

        action = Action.create("engageEntity", {
            "target_spec": "nearest_hostile",
            "attack_range": 2.0,
            "cooldown_ticks": 10,
            "acquire_radius": 32,
            "grid_radius": 32,
            "max_expand": 200,
            "timeout_ticks": 600,
            "disengage_health": 6.0,
        })
        result = body.execute(action)
        if not (result.ok and result.accepted):
            rcon.command(f"player {BOT} kill"); rcon.command("kill @e[type=skeleton]")
            raise AssertionError(f"body rejected engageEntity: {result}")

        term_box = {}
        def awaiter():
            term_box["terminal"] = body.await_action_terminal(
                action.id, timeout_s=40.0, terminal_events={"engageDone", "death", "respawned"}
            )

        th = threading.Thread(target=awaiter)
        th.start()

        t0 = time.monotonic()
        max_z = 20.0
        samples = []
        while th.is_alive() and time.monotonic() - t0 < 40.5:
            bpos = body.get_state().pos
            z = round(bpos[2], 2)
            if z > max_z:
                max_z = z
            samples.append((round(time.monotonic() - t0, 2), round(bpos[0], 2), z))
            time.sleep(0.5)
        th.join(timeout=45.0)

        terminal = term_box.get("terminal")
        rcon.command(f"player {BOT} kill"); time.sleep(0.2)
        rcon.command("kill @e[type=skeleton]")

        if terminal is None:
            raise AssertionError("no terminal event received")
        td = terminal.data
        reason = str(td.get("reason") or "")
        print(f"TERMINAL: name={terminal.name} reason={reason} attacks={td.get('attacks')} success={td.get('success')}")
        print(f"max_z reached: {max_z} (direct path would stay ~20.0; cover lane is z=22)")
        print(f"samples (t, x, z): {samples[:8]}{' ...' if len(samples) > 8 else ''}")

        if reason != "killed":
            raise AssertionError(f"expected reason=killed, got {reason}")
        if max_z < 21.0:
            raise AssertionError(f"bot did not take the cover lane (max_z={max_z} < 21); cover-A* may not be engaging")
        print("\nCOVER ENGAGE CONFIRMED: bot took the covered z=22 lane AND killed the ranged skeleton.")


if __name__ == "__main__":
    main()
