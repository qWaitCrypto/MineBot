#!/usr/bin/env python3
"""Decisive flint&steel fire-PERSISTENCE test.

Settles whether physical `player use once` produces a PERSISTENT, observable
fire block when aimed at a sturdy top face (the only geometry where vanilla
FireBlock.canSurvive keeps the fire). Reads the exact target cell with zero/
minimal delay and again after 1s, plus durability, so we can distinguish:
  * never placed (no durability change), vs
  * placed-then-removed (durability change, fire gone), vs
  * placed-and-persists (durability change, fire stays) -> physical path WORKS.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/qwait/MineBot")

from minebot.game.rcon import RconClient, RconConfig
from minebot.game import ScarpetBody

BOT = "FSProbe"


def main():
    r = RconClient(RconConfig())
    r.connect()

    def c(cmd, d=0.0):
        o = r.command(cmd)
        if d:
            time.sleep(d)
        return o

    def is_fire(x, y, z):
        return "F_HIT" in c(f"execute if block {x} {y} {z} fire run say F_HIT")

    with r:
        c(f"player {BOT} kill", 0.3)
        c("gamerule doFireTick false", 0.05)
        c("time set day", 0.05)
        c("weather clear", 0.05)
        c("fill -6 67 -6 6 85 6 air", 0.1)
        c("fill -6 68 -6 6 68 6 netherrack", 0.1)  # floor blocks y=68, top surface y=69
        body = ScarpetBody(BOT, r)
        res = body.spawn((0, 69, 0))
        print("spawn ok:", res.ok and res.accepted)
        c(f"gamemode survival {BOT}", 0.1)
        c(f"effect give {BOT} minecraft:fire_resistance 9999 5 true", 0.1)

        # Multiple aims at TOP faces of floor blocks at increasing offsets, so at
        # least one ray cleanly hits a top face (clickedFace=UP -> fire on a
        # sturdy-supported air cell that canSurvive keeps).
        trials = [
            ("aim(2,68,0).top", (2, 68, 0), (2.5, 68.99, 0.5), (2, 69, 0)),
            ("aim(1,68,0).top", (1, 68, 0), (1.5, 68.99, 0.5), (1, 69, 0)),
            ("aim(3,68,0).top", (3, 68, 0), (3.5, 68.99, 0.5), (3, 69, 0)),
            ("aim(0,68,2).top", (0, 68, 2), (0.5, 68.99, 2.5), (0, 69, 2)),
        ]
        results = {}
        for label, _click, look, fire_cell in trials:
            c("fill -6 69 -6 6 80 6 air", 0.05)        # clear any prior fire
            c("fill -6 68 -6 6 68 6 netherrack", 0.05)  # restore floor
            c(f"item replace entity {BOT} hotbar.0 with flint_and_steel 1", 0.1)
            c(f"player {BOT} hotbar 1", 0.1)
            c(f"tp {BOT} 0.5 69 0.5", 0.15)
            c(f"player {BOT} look at {look[0]} {look[1]} {look[2]}", 0.25)
            before = c(f"data get entity {BOT} SelectedItem").strip()
            c(f"player {BOT} use once", 0.12)  # ~2 ticks
            fx, fy, fz = fire_cell
            fire_imm = is_fire(fx, fy, fz)
            after = c(f"data get entity {BOT} SelectedItem").strip()
            dmg = ("damage" in after.lower()) and ("damage" not in before.lower())
            time.sleep(1.0)
            fire_1s = is_fire(fx, fy, fz)
            results[label] = (dmg, fire_imm, fire_1s)
            print(f"[{label}] durability_used={dmg}  fire_immediate={fire_imm}  fire_after_1s={fire_1s}  cell={fire_cell}")

        print("\n=== VERDICT ===")
        any_persist = any(f1s for _, _, f1s in results.values())
        any_dmg = any(d for d, _, _ in results.values())
        any_imm = any(fi for _, fi, _ in results.values())
        if any_persist:
            print(">>> PHYSICAL fire PERSISTS — physical use_on_block WORKS. Rip out the substitute.")
        elif any_imm:
            print(">>> fire placed-then-removed (immediate yes, 1s no) — canSurvive/fireTick removal. "
                  "Physical placement happens but isn't durable; substitute justified, root cause known.")
        elif any_dmg:
            print(">>> durability consumed but fire NEVER observable even immediately — "
                  "setBlock happens but fire is structurally rejected. Substitute justified.")
        else:
            print(">>> use never even registered (no durability) — aim/raycast problem, NOT ignition. "
                  "Investigate aim before concluding anything about ignition.")


if __name__ == "__main__":
    main()
