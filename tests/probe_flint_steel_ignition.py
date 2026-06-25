#!/usr/bin/env python3
"""Controlled flint&steel physical-ignition ground-truth probe (v2).

v1 was INVALID: it hand-rolled `/player NAME spawn ... facing x y z`, which is
wrong Carpet syntax, so the bot never spawned and every durability read returned
"No entity was found" — a vacuous "no ignition" verdict from a broken instrument.

v2 fixes the instrument:
  * spawn via the PROVEN ScarpetBody.spawn() (Scarpet minebot_spawn), not raw cmd
  * HARD PRECONDITION GATE: assert the bot exists AND its held flint&steel NBT is
    readable BEFORE any ignition trial. If not, HALT — do not emit a verdict.
  * aim via `player NAME look at <x> <y> <z>` (raycast at a real block face)
  * durability (un-fakeable proof of a real use_on_block) read from SelectedItem NBT

Only after the gate passes do we trust "fire appeared / didn't" and
"durability changed / didn't" as physical ground truth.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/qwait/MineBot")

from minebot.game.rcon import RconClient, RconConfig
from minebot.game import ScarpetBody

BOT = "FSProbe"
STALE = ["E2EUseOnBot", "E2ELosBot", "E2EGiveBot", "E2ERecvBot", BOT]


def cmd(r, c, delay=0.05):
    out = r.command(c)
    if delay:
        time.sleep(delay)
    return out


def cleanup(r):
    for name in STALE:
        cmd(r, f"player {name} kill", delay=0.15)


def setup_world(r):
    cmd(r, "carpet commandPlayer true")
    cmd(r, "carpet allowSpawningOfflinePlayers true")
    cmd(r, "script in minebot run minebot_reset()", delay=0.2)
    cmd(r, "gamerule doDaylightCycle false")
    cmd(r, "gamerule doFireTick false")
    cmd(r, "time set day")
    cmd(r, "fill -4 69 -4 4 80 4 air")
    cmd(r, "fill -4 68 -4 4 68 4 netherrack")  # floor block layer (top surface y=69)


def selected_item(r):
    return cmd(r, f"data get entity {BOT} SelectedItem", delay=0.0).strip()


def has_fs(r):
    s = selected_item(r)
    return "flint_and_steel" in s, s


def fire_cells_near(r, cx, cy, cz, span=2):
    hits = []
    for dx in range(-span, span + 1):
        for dy in range(-span, span + 1):
            for dz in range(-span, span + 1):
                x, y, z = cx + dx, cy + dy, cz + dz
                o = cmd(r, f"execute if block {x} {y} {z} fire run say HIT", delay=0.0)
                if "HIT" in o:
                    hits.append((x, y, z))
    return hits


def precondition_gate(r, body):
    """Spawn the bot and PROVE the instrument works before any trial."""
    cmd(r, "fill -4 69 -4 4 80 4 air")
    cmd(r, "fill -4 68 -4 4 68 4 netherrack")
    res = body.spawn((0, 69, 0))
    if not (res.ok and res.accepted):
        raise SystemExit(f"GATE FAIL: spawn rejected: {res}")
    cmd(r, f"gamemode survival {BOT}")
    cmd(r, f"effect give {BOT} minecraft:fire_resistance 9999 5 true")  # survive own fire
    cmd(r, f"item replace entity {BOT} hotbar.0 with flint_and_steel 1", delay=0.1)
    cmd(r, f"player {BOT} hotbar 1", delay=0.2)  # select slot 0 (1-indexed)
    ok, nbt = has_fs(r)
    print(f"GATE: bot exists, SelectedItem readable={bool(nbt and 'No entity' not in nbt)}")
    print(f"GATE: SelectedItem = {nbt[:200]}")
    if not ok:
        raise SystemExit("GATE FAIL: flint&steel not readable in hand after give+select — "
                         "instrument still broken, refusing to emit a verdict.")
    print("GATE PASS: instrument verified. Proceeding to ignition trials.\n")


def trial(r, label, *, stand, look_at, fire_center):
    # Reset surface + clear fire; re-give a pristine flint&steel each trial.
    cmd(r, "fill -4 69 -4 4 80 4 air")
    cmd(r, "fill -4 68 -4 4 68 4 netherrack")
    cmd(r, f"item replace entity {BOT} hotbar.0 with flint_and_steel 1", delay=0.1)
    cmd(r, f"player {BOT} hotbar 1", delay=0.1)
    sx, sy, sz = stand
    cmd(r, f"tp {BOT} {sx} {sy} {sz}", delay=0.15)
    cmd(r, f"player {BOT} look at {look_at[0]} {look_at[1]} {look_at[2]}", delay=0.2)
    nbt_before = selected_item(r)
    cmd(r, f"player {BOT} use once", delay=0.6)
    nbt_after = selected_item(r)
    fires = fire_cells_near(r, *fire_center)
    durability_changed = (nbt_before != nbt_after) and "No entity" not in nbt_before
    print(f"[{label}] stand={stand} look_at={look_at}")
    print(f"    nbt_before_has_damage={'damage' in nbt_before}  nbt_after_has_damage={'damage' in nbt_after}  changed={durability_changed}")
    print(f"    fire_cells={fires}")
    print(f"    => ignition={'YES' if fires else 'NO'}  durability_changed={'YES' if durability_changed else 'NO'}")
    return bool(fires), durability_changed


def main():
    r = RconClient(RconConfig())
    r.connect()
    with r:
        cleanup(r)
        setup_world(r)
        body = ScarpetBody(BOT, r)
        precondition_gate(r, body)

        results = {}
        # Floor block layer at y=68 (top surface y=69). Bot stands feet at y=69.
        # T1: aim straight down at floor cell (1,68,0) top face -> fire at (1,69,0)
        results["T1_lookdown_floor"] = trial(
            r, "T1_lookdown_floor",
            stand=(0, 69, 0), look_at=(1, 68, 0), fire_center=(1, 69, 0))
        # T2: place a pillar, aim at its side face
        cmd(r, "setblock 2 69 0 netherrack")
        results["T2_side_pillar"] = trial(
            r, "T2_side_pillar",
            stand=(0, 69, 0), look_at=(2, 69, 0), fire_center=(1, 69, 0))
        # T3: aim down at the cell directly under the bot's feet (0,68,0) -> fire at (0,69,0)
        results["T3_under_feet"] = trial(
            r, "T3_under_feet",
            stand=(0, 69, 0), look_at=(0, 68, 0), fire_center=(0, 69, 0))

        print("\n=== SUMMARY ===")
        any_fire = any(f for f, _ in results.values())
        any_dura = any(d for _, d in results.values())
        for k, (f, d) in results.items():
            print(f"  {k:22s} fire={f} durability_changed={d}")
        print(f"\nVERDICT: physical_ignition_ever={'YES' if any_fire else 'NO'}  "
              f"durability_ever_changed={'YES' if any_dura else 'NO'}")
        if any_fire:
            print(">>> PHYSICAL flint&steel ignition WORKS with correct aim — "
                  "the substitute is UNNECESSARY; close use_on_block via the physical path.")
        elif any_dura:
            print(">>> use fired (durability lost) but fire didn't persist — "
                  "root-cause is fire placement/fireTick, NOT the use itself.")
        else:
            print(">>> CONFIRMED ON A VERIFIED INSTRUMENT: no ignition, no durability loss "
                  "across all aims — physical flint&steel use is genuinely inert. Substitute justified.")


if __name__ == "__main__":
    main()
