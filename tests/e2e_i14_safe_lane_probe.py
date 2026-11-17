#!/usr/bin/env python3
"""Decide whether a genuinely-safe swim lane exists across the golden frontier.

Phase A proved I14 prunes 6 water cells near the lava field.  This probe asks
the sharper question: is there ANY water crossing lane whose cells are safe by
the reflex's own criterion (no lava in the feet-centered neighborhood of the
swimming body's feet AND head), i.e. cells the bot could actually swim without
the survival reflex preempting?

- If safe lanes exist that I14 prunes  -> I14 over-prunes: fix the predicate.
- If every water cell bridging the gap is reflex-unsafe -> the barrier is a real
  lava-flanked crossing: the fix is governed break/place mobility, not the
  predicate, and I14 only made the failure honest (plan-time no_path).

Read-only, mutates nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game.rcon import RconClient, RconConfig  # noqa: E402


def _val(raw: str) -> str:
    if " = " in raw:
        return raw.split(" = ", 1)[1].rsplit(" (", 1)[0].strip()
    return raw.strip()


def main() -> int:
    c = RconClient(RconConfig())
    c.connect()

    def run(expr: str) -> str:
        return _val(c.request(f"script in minebot run {expr}"))

    # Bounding box spanning bot last pos (-40,63,-27) to frontier (-52,66,-24).
    x0, x1 = -56, -38
    z0, z1 = -30, -18
    y0, y1 = 61, 67

    water_cells = []
    safe_water = []  # water/air column with NO lava in reflex neighborhood of feet+head
    reflex_unsafe_water = []
    for x in range(x0, x1 + 1):
        for z in range(z0, z1 + 1):
            for y in range(y0, y1 + 1):
                fb = run(f"'' + block({x},{y},{z})")
                if "water" not in fb:
                    continue
                hb = run(f"'' + block({x},{y}+1,{z})")
                if hb not in ("air", "minecraft:air", "water", "minecraft:water"):
                    continue
                water_cells.append([x, y, z])
                # reflex criterion: lava in feet-centered box of BOTH feet and head layers
                lava_feet = run(f"lava_near_pos(l({x}+0.5,{y},{z}+0.5))")
                lava_head = run(f"lava_near_pos(l({x}+0.5,{y}+1,{z}+0.5))")
                if lava_feet == "false" and lava_head == "false":
                    safe_water.append([x, y, z])
                else:
                    reflex_unsafe_water.append(
                        {"cell": [x, y, z], "lava_feet": lava_feet, "lava_head": lava_head}
                    )
    c.close()

    report = {
        "water_cells_in_gap": len(water_cells),
        "reflex_safe_water_cells": len(safe_water),
        "reflex_unsafe_water_cells": len(reflex_unsafe_water),
        "safe_water_sample": safe_water[:30],
        "unsafe_water_sample": reflex_unsafe_water[:15],
        "verdict": (
            "SAFE_LANE_EXISTS: I14 over-prunes reachable safe water; fix predicate"
            if safe_water
            else "NO_SAFE_LANE: crossing is genuinely lava-flanked; fix = governed mutation crossing, I14 only made failure honest"
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
