#!/usr/bin/env python3
"""Focused live probe for production rangedAttack against a supported crystal."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "CrystalProbeBot"
BASE = (240, 59, 0)
CRYSTAL = (240.5, 60.0, 14.5)


def command(rcon, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon) -> None:
    x, y, z = BASE
    for cmd in [
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doMobSpawning false",
        "gamerule doWeatherCycle false",
        "weather clear",
        "difficulty normal",
        "time set day",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "script in minebot run minebot_reset()",
        "script in minebot run global_reflex_scan = false",
        f"fill {x-4} {y} {z-4} {x+4} {y+8} {z+20} air",
        f"fill {x-4} {y-1} {z-4} {x+4} {y-1} {z+20} stone",
        f"setblock {int(CRYSTAL[0])} {int(CRYSTAL[1]) - 1} {int(CRYSTAL[2])} obsidian",
        f"summon end_crystal {CRYSTAL[0]} {CRYSTAL[1]} {CRYSTAL[2]}",
    ]:
        command(rcon, cmd)


def crystal_exists(rcon) -> bool:
    raw = command(rcon, "data get entity @e[type=end_crystal,limit=1] Pos", delay=0.0)
    return "No entity was found" not in raw


def arrow_pos(rcon):
    raw = command(rcon, "data get entity @e[type=arrow,sort=nearest,limit=1] Pos", delay=0.0)
    if "No entity was found" in raw:
        return None
    tail = raw.rsplit("[", 1)[-1].split("]", 1)[0]
    vals = [float(part.strip().rstrip("d")) for part in tail.split(",")]
    return [round(v, 4) for v in vals]


def main() -> int:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, BASE)
        command(rcon, f"tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]} 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
        command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
        command(rcon, f"player {BOT} hotbar 1")
        event = body.ranged_attack(
            weapon="bow",
            target_type="minecraft:end_crystal",
            radius=24,
            timeout_ticks=120,
            use_interval_ticks=22,
            expected_shots=1,
            timeout_s=12.0,
        )
        print(
            {
                "event": event.name,
                "data": event.data,
                "crystal_alive": crystal_exists(rcon),
                "arrow_pos": arrow_pos(rcon),
            }
        )
        command(rcon, f"player {BOT} kill", delay=0.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
