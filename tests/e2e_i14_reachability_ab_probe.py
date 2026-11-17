#!/usr/bin/env python3
"""Decisive reachability test using the REAL Scarpet planner (read-only).

From the bot's actual stall position toward each frontier stand, run
navigate_to_goals_plan under two real profiles built from
navigation_context_from_params:

  AQUATIC  : swim + aquatic_traversal, no mutation (what explore_for uses today)
  GOVERNED : swim + aquatic + break/place/pillar/downward budgets on
             (what resource collection escalates to)

If GOVERNED reaches goals AQUATIC cannot, the fix is to give explore_for the
governed escalation profile.  If neither reaches, the crossing is a genuine
capability gap for both consumers and the classification is a mechanism Debt.

Planning only; never moves or mutates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game.rcon import RconClient, RconConfig  # noqa: E402

START = (-40, 63, -27)
GOALS = [(-52, 66, -24), (-56, 74, -24), (-40, 70, -8), (-24, 70, -24)]

AQUATIC = "{'allow_swim'->true,'aquatic_traversal'->true}"
GOVERNED = (
    "{'allow_swim'->true,'aquatic_traversal'->true,"
    "'allow_break'->true,'break_budget'->8,"
    "'allow_place'->true,'place_budget'->8,'scaffold_count'->64,"
    "'allow_pillar'->true,'pillar_budget'->8,"
    "'allow_downward'->true,'downward_budget'->8}"
)


def _val(raw: str) -> str:
    if " = " in raw:
        return raw.split(" = ", 1)[1].rsplit(" (", 1)[0].strip()
    return raw.strip()


def main() -> int:
    c = RconClient(RconConfig())
    c.connect()

    def run(expr: str) -> str:
        return _val(c.request(f"script in minebot run {expr}"))

    sx, sy, sz = START
    results = {}
    for label, params in (("AQUATIC", AQUATIC), ("GOVERNED", GOVERNED)):
        per_goal = []
        for (gx, gy, gz) in GOALS:
            expr = (
                f"(ctx = navigation_context_from_params({params});"
                f" plan = navigate_to_goals_plan({sx},{sy},{sz},"
                f" l(l({gx},{gy},{gz},1)), 48, 6000, 3, 3, null, 1, ctx);"
                f" str('status=%s|steps=%s|expanded=%s|selected=%s|partial_dist=%s',"
                f" plan:1, length(plan:3), plan:2, plan:4, plan:6))"
            )
            raw = run(expr)
            per_goal.append({"goal": [gx, gy, gz], "plan": raw[:300]})
        results[label] = per_goal
    c.close()
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
