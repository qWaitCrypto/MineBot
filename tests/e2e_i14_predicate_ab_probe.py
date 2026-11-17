#!/usr/bin/env python3
"""Read-only A/B falsification for the I14 navigation_lava_unsafe change.

For every cell in a volume around the frontier where run ag-20260723-060009
stalled, evaluate the OLD predicate (lava_near_pos(feet) || is_lava_at(head))
and the NEW I14 predicate (adds: any LIQUID feet/head with lava in the
head-layer neighborhood -> unsafe).  Report only DISAGREEMENTS and classify
each by whether the rejected cell is genuine lava or water.

If every disagreement is genuine lava, I14 did not over-tighten this terrain
and the stall is a routing/recovery gap, not the predicate.  If NEW rejects
water cells the OLD predicate allowed, I14 pruned necessary swim edges.

This is a bounded read-only probe; it mutates nothing and is not a gate run.
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

    # Volume around the stall frontier and the lava corridor.
    x0, x1 = -60, -20
    z0, z1 = -44, -4
    y0, y1 = 60, 76
    disagreements = []
    scanned = 0
    for x in range(x0, x1 + 1, 2):
        for z in range(z0, z1 + 1, 2):
            for y in range(y0, y1 + 1):
                scanned += 1
                old = run(
                    f"(lava_near_pos(l({x}+0.5,{y},{z}+0.5)) || is_lava_at({x},{y}+1,{z}))"
                )
                new = run(f"navigation_lava_unsafe({x},{y},{z})")
                if old != new:
                    fb = run(f"'' + block({x},{y},{z})")
                    hb = run(f"'' + block({x},{y}+1,{z})")
                    disagreements.append(
                        {
                            "cell": [x, y, z],
                            "old_unsafe": old,
                            "new_unsafe": new,
                            "feet_block": fb,
                            "head_block": hb,
                        }
                    )
    c.close()

    water_rejects = [
        d
        for d in disagreements
        if d["new_unsafe"] == "true"
        and "lava" not in d["feet_block"]
        and "lava" not in d["head_block"]
    ]
    report = {
        "scanned_cells": scanned,
        "disagreement_count": len(disagreements),
        "new_rejects_non_lava_count": len(water_rejects),
        "new_rejects_non_lava_sample": water_rejects[:20],
        "all_disagreements_sample": disagreements[:20],
        "verdict": (
            "I14_IMPLICATED: NEW predicate prunes non-lava (water/other) cells"
            if water_rejects
            else "I14_EXONERATED_ON_THIS_TERRAIN: all disagreements are genuine lava"
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
