#!/usr/bin/env python3
"""Live probe: validate server-side engageEntity end-to-end (S2+S3).

Spawns a bot and a NoAI husk. Dispatches engageEntity manually (not via the
tool layer) so the raw engageDone terminal + a bot/husk timeline are visible.
Asserts the bot approached, swung, killed the husk (engageDone reason=killed,
attacks>0), and the husk is gone afterward.
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

BOT = "EngageProbe"


def _husk_state(rcon):
    count = rcon.command("script run length(entity_selector('@e[type=husk]'))")
    health = rcon.command("script run query(entity_selector('@e[type=husk,limit=1]'):0,'health')")
    return f"count={count} hp={health}"


def main():
    with connect_or_skip() as rcon:
        for cmd in [
            "script unload minebot", "script load minebot global",
            "carpet commandPlayer true", "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false", "time set day", "weather clear",
            f"player {BOT} kill", "kill @e[type=husk]",
            "fill 20 69 20 28 76 28 air",
            "fill 20 69 20 28 69 28 stone",
        ]:
            rcon.command(cmd)
            time.sleep(0.05)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (20, 70, 20))
        rcon.command("summon husk 24 70 24 {NoAI:1b,PersistenceRequired:1b,Health:20f}")
        time.sleep(0.5)

        action = Action.create("engageEntity", {
            "target_spec": "nearest_hostile",
            "attack_range": 2.0,
            "cooldown_ticks": 10,
            "acquire_radius": 32,
            "grid_radius": 32,
            "max_expand": 200,
            "timeout_ticks": 500,
            "disengage_health": 6.0,
        })
        result = body.execute(action)
        print(f"execute: ok={result.ok} accepted={result.accepted} error={result.error}")
        if not (result.ok and result.accepted):
            rcon.command(f"player {BOT} kill"); rcon.command("kill @e[type=husk]")
            raise AssertionError("body rejected engageEntity")

        term_box = {}
        def awaiter():
            term_box["terminal"] = body.await_action_terminal(
                action.id, timeout_s=30.0, terminal_events={"engageDone", "death", "respawned"}
            )

        th = threading.Thread(target=awaiter)
        th.start()

        t0 = time.monotonic()
        while th.is_alive() and time.monotonic() - t0 < 30.5:
            bpos = tuple(round(v, 2) for v in body.get_state().pos)
            print(f"  t={time.monotonic()-t0:5.2f} bot={bpos} husk={_husk_state(rcon)}")
            time.sleep(1.0)
        th.join(timeout=35.0)

        terminal = term_box.get("terminal")
        rcon.command(f"player {BOT} kill"); time.sleep(0.2)
        rcon.command("kill @e[type=husk]")

        if terminal is None:
            raise AssertionError("no terminal event received")
        td = terminal.data
        print(f"\nTERMINAL: name={terminal.name}")
        print(f"  data={td}")
        reason = str(td.get("reason") or "")
        attacks = td.get("attacks")
        print(f"  reason={reason} attacks={attacks} success={td.get('success')} target_health={td.get('target_health')}")

        if reason != "killed":
            raise AssertionError(f"expected reason=killed, got {reason}")
        if not attacks or int(attacks) <= 0:
            raise AssertionError(f"expected attacks>0, got {attacks}")
        print("\nENGAGE CONFIRMED: bot approached, swung, killed the husk.")


if __name__ == "__main__":
    main()
