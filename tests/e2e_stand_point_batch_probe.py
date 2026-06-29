#!/usr/bin/env python3
"""Live probe: confirm target-centered stand-point selection now uses one
blockCells batch round-trip instead of N per-cell blockAt reads.

Places a stone floor + a dirt target, calls interaction_stand_points and
_best_mining_stand_candidate through the real ScarpetBody, and counts
perceptions by scope. The win is mechanical (1 batch vs ~12-36 per-cell),
but this pins it concretely on the real server.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body.interaction_support import interaction_stand_points
from minebot.body.block_work import _best_mining_stand_candidate
from minebot.game import RconClient, ScarpetBody
from minebot.game.rcon import RconConfig
from tests.e2e_support import SKIP_EXIT_CODE, connect_or_skip, spawn_or_fail


BOT = "StandBatchProbe"


def command(rcon, text, delay=0.05):
    out = rcon.command(text)
    if delay:
        time.sleep(delay)
    return out


def counting_perceive(body):
    counts = {"blockAt": 0, "blockCells": 0, "other": 0}
    orig = body.perceive

    def wrapped(scope, params):
        if scope == "blockAt":
            counts["blockAt"] += 1
        elif scope == "blockCells":
            counts["blockCells"] += 1
        else:
            counts["other"] += 1
        return orig(scope, params)

    body.perceive = wrapped
    return counts


def main():
    with connect_or_skip() as rcon:
        for cmd in [
            "script unload minebot", "script load minebot global",
            "carpet commandPlayer true", "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false", "time set day", "weather clear",
            f"player {BOT} kill",
            "fill 20 70 20 24 72 24 air",
            "fill 20 69 20 24 69 24 stone",
            "setblock 22 70 22 dirt",
        ]:
            command(rcon, cmd)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (20, 70, 20))
        time.sleep(0.4)

        target = (22, 70, 22)

        counts = counting_perceive(body)
        stands = interaction_stand_points(body, target)
        ipc = dict(counts)

        counts2 = counting_perceive(body)
        best = _best_mining_stand_candidate(body, target, (20.5, 70.0, 20.5))
        mining = dict(counts2)

        command(rcon, f"player {BOT} kill", delay=0.2)

        print("\n=== STAND-POINT BATCH ROUND-TRIP PROBE ===")
        print(f"target={target}")
        print(f"interaction_stand_points -> {len(stands)} stand(s): {stands}")
        print(f"  perceptions: blockAt={ipc['blockAt']} blockCells={ipc['blockCells']} other={ipc['other']}")
        print(f"  round-trips saved: ~{ipc['blockAt'] + 3 * max(len(stands), 1) - (1 if ipc['blockCells'] else 0)} (vs old ~{3 * 5} per-cell blockAt)")
        print(f"_best_mining_stand_candidate -> best={best}")
        print(f"  perceptions: blockAt={mining['blockAt']} blockCells={mining['blockCells']} other={mining['other']}")

        ok = ipc["blockAt"] == 0 and ipc["blockCells"] >= 1 and mining["blockAt"] == 0 and mining["blockCells"] >= 1
        if not ok:
            raise AssertionError(f"expected no per-cell blockAt (only batched blockCells), got interaction={ipc} mining={mining}")
        if not stands:
            raise AssertionError(f"no stand points found for {target}")
        print(f"\nCONFIRMED: stand-point selection now batched blockCells only (interaction={ipc['blockCells']} call(s), mining={mining['blockCells']} call(s) for 36 cells) — was ~12-36 per-cell blockAt.")


if __name__ == "__main__":
    main()
