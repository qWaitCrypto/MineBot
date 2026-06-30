#!/usr/bin/env python3
"""Live probe: validate Scarpet find-hostiles perception (S1).

Spawns a bot on a forceloaded platform with a near zombie, a far skeleton
(ranged), and a near cow (passive). Asserts nearbyHostiles returns the two
hostiles sorted nearest-first and excludes the passive mob, and that
is_ranged_hostile classifies the skeleton but not the zombie.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import ScarpetBody  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402

BOT = "FindHostProbe"


def main():
    with connect_or_skip() as rcon:
        for cmd in [
            "script unload minebot", "script load minebot global",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false", "time set 18000", "weather clear",
            f"player {BOT} kill",
            "kill @e[type=zombie]", "kill @e[type=skeleton]", "kill @e[type=cow]",
            "fill 20 69 20 28 76 28 air",
            "fill 20 69 20 28 69 28 stone",
        ]:
            rcon.command(cmd)
            time.sleep(0.05)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (20, 70, 20))
        time.sleep(0.4)
        rcon.command("summon zombie 22 70 20 {NoAI:1b,PersistenceRequired:1b}")
        rcon.command("summon skeleton 26 70 20 {NoAI:1b,PersistenceRequired:1b}")
        rcon.command("summon cow 21 70 20 {NoAI:1b,PersistenceRequired:1b}")
        time.sleep(0.5)

        p = body.perceive("nearbyHostiles", {"radius": 10, "limit": 8})
        if not p.ok:
            raise AssertionError(f"nearbyHostiles perception failed: {p.error}")
        entities = p.data.get("entities") or []
        types = [e.get("type") for e in entities]
        print(f"nearbyHostiles: count={p.data.get('count')} types={types}")

        if "zombie" not in types or "skeleton" not in types:
            raise AssertionError(f"expected zombie+skeleton in hostiles, got {types}")
        if "cow" in types:
            raise AssertionError("cow (passive) leaked into hostiles")

        dists = [(e.get("type"), round(e.get("dist2") or 0, 2)) for e in entities]
        zombie_d = next(d for t, d in dists if t == "zombie")
        skel_d = next(d for t, d in dists if t == "skeleton")
        if zombie_d > skel_d:
            raise AssertionError(f"not sorted nearest-first: zombie dist2={zombie_d} > skeleton dist2={skel_d}")
        print(f"sorted nearest-first OK: zombie dist2={zombie_d} < skeleton dist2={skel_d}")

        skel_ranged = rcon.command("script in minebot run is_ranged_hostile(entity_selector('@e[type=skeleton,limit=1]'):0)")
        zomb_ranged = rcon.command("script in minebot run is_ranged_hostile(entity_selector('@e[type=zombie,limit=1]'):0)")
        print(f"is_ranged_hostile: skeleton={skel_ranged} zombie={zomb_ranged}")
        if "true" not in skel_ranged:
            raise AssertionError("skeleton should be ranged")
        if "false" not in zomb_ranged:
            raise AssertionError("zombie should not be ranged")

        rcon.command(f"player {BOT} kill")
        rcon.command("kill @e[type=zombie]"); rcon.command("kill @e[type=skeleton]"); rcon.command("kill @e[type=cow]")
        print("\nFIND_HOSTILES CONFIRMED: hostiles returned sorted, passive excluded, ranged classification correct.")


if __name__ == "__main__":
    main()
