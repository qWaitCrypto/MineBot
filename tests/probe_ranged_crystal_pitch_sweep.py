#!/usr/bin/env python3
"""Focused live pitch sweep for fake-player bow shots against a supported crystal."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "AimProbeBot"
BASE = (240, 59, 0)
CRYSTAL = (240.5, 60.0, 14.5)
PITCHES = [-45, -40, -35, -30, -25, -20, -15, -10, -5, 0, 5, 10, 15, 20, 25, 30]


def command(rcon, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon) -> ScarpetBody:
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
        f"fill {x-4} {y} {z-4} {x+4} {y+8} {z+24} air",
        f"fill {x-4} {y-1} {z-4} {x+4} {y-1} {z+24} stone",
        f"setblock {int(CRYSTAL[0])} {int(CRYSTAL[1]) - 1} {int(CRYSTAL[2])} obsidian",
        f"summon end_crystal {CRYSTAL[0]} {CRYSTAL[1]} {CRYSTAL[2]}",
    ]:
        command(rcon, cmd)
    body = ScarpetBody(BOT, rcon)
    spawn_or_fail(body, BASE)
    for cmd in [
        f"tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]} 0 0",
        f"gamemode survival {BOT}",
        f"clear {BOT}",
        f"item replace entity {BOT} hotbar.0 with bow",
        f"item replace entity {BOT} weapon.offhand with arrow 16",
        f"player {BOT} hotbar 1",
        f"player {BOT} stop",
    ]:
        command(rcon, cmd)
    return body


def crystal_alive(rcon) -> bool:
    raw = command(rcon, "data get entity @e[type=end_crystal,limit=1] Pos", delay=0.0)
    return "No entity was found" not in raw


def arrow_pos(rcon) -> list[float] | None:
    raw = command(rcon, "data get entity @e[type=arrow,sort=nearest,limit=1] Pos", delay=0.0)
    if "No entity was found" in raw:
        return None
    tail = raw.rsplit("[", 1)[-1].split("]", 1)[0]
    return [float(part.strip().rstrip("d")) for part in tail.split(",")]


def shoot_once(rcon, pitch: int) -> dict[str, object]:
    command(rcon, "kill @e[type=arrow]", delay=0.05)
    command(rcon, f"tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]} 0 0")
    command(rcon, f"player {BOT} look {pitch} 0", delay=0.05)
    command(rcon, f"player {BOT} use continuous", delay=0.05)
    time.sleep(1.15)
    command(rcon, f"player {BOT} stop", delay=0.02)

    samples: list[list[float]] = []
    for _ in range(50):
        pos = arrow_pos(rcon)
        if pos is not None:
            samples.append(pos)
        if not crystal_alive(rcon):
            break
        time.sleep(0.05)

    alive = crystal_alive(rcon)
    best = None
    if samples:
        best = min(
            samples,
            key=lambda pos: (pos[0] - CRYSTAL[0]) ** 2
            + (pos[1] - CRYSTAL[1]) ** 2
            + (pos[2] - CRYSTAL[2]) ** 2,
        )
    return {
        "pitch": pitch,
        "crystal_alive": alive,
        "best": [round(value, 3) for value in best] if best is not None else None,
        "max_z": round(max(pos[2] for pos in samples), 3) if samples else None,
        "max_y": round(max(pos[1] for pos in samples), 3) if samples else None,
        "samples": len(samples),
    }


def main() -> int:
    with connect_or_skip() as rcon:
        for pitch in PITCHES:
            setup_world(rcon)
            result = shoot_once(rcon, pitch)
            print(result, flush=True)
            command(rcon, f"player {BOT} kill", delay=0.0)
            if result["crystal_alive"] is False:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
