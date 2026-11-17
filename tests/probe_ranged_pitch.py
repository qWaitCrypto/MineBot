#!/usr/bin/env python3
"""Focused live probe for yaw/pitch-based fake-player bow aiming."""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "PitchProbeBot"
BASE = (240, 59, 0)
CRYSTAL = (240.5, 59.0, 14.5)


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
        f"fill {x-4} {y-1} {z-4} {x+4} {y+8} {z+20} air",
        f"fill {x-4} {y-2} {z-4} {x+4} {y-2} {z+20} stone",
    ]:
        command(rcon, cmd)


def summon_bot(rcon) -> None:
    command(rcon, f"player {BOT} spawn at {BASE[0]} {BASE[1]} {BASE[2]} facing 0 0")
    command(rcon, f"tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]} 0 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
    command(rcon, f"player {BOT} hotbar 1")
    command(rcon, f"player {BOT} stop")


def spawn_crystal(rcon) -> None:
    command(rcon, "kill @e[type=end_crystal]", delay=0.0)
    command(rcon, f"setblock {int(CRYSTAL[0])} {int(CRYSTAL[1]) - 1} {int(CRYSTAL[2])} obsidian", delay=0.0)
    command(rcon, f"summon end_crystal {CRYSTAL[0]} {CRYSTAL[1]} {CRYSTAL[2]}", delay=0.1)


def entity_exists(rcon, selector: str) -> bool:
    return "exists" in command(rcon, f"execute if entity {selector} run say exists", delay=0.0)


def nearest_arrow_pos(rcon) -> tuple[float, float, float] | None:
    raw = command(rcon, "data get entity @e[type=arrow,sort=nearest,limit=1] Pos", delay=0.0)
    if "No entity was found" in raw:
        return None
    tail = raw.rsplit("[", 1)[-1].split("]", 1)[0]
    vals = [float(part.strip().rstrip("d")) for part in tail.split(",")]
    return (vals[0], vals[1], vals[2])


def wait_arrow(rcon, timeout_s: float = 3.0) -> tuple[float, float, float]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pos = nearest_arrow_pos(rcon)
        if pos is not None:
            return pos
        time.sleep(0.05)
    raise AssertionError("timed out waiting for arrow")


def yaw_pitch_to(src: tuple[float, float, float], dst: tuple[float, float, float]) -> tuple[float, float]:
    dx = dst[0] - src[0]
    dy = dst[1] - src[1]
    dz = dst[2] - src[2]
    yaw = -math.degrees(math.atan2(dx, dz))
    horiz = math.sqrt(dx * dx + dz * dz)
    pitch = -math.degrees(math.atan2(dy, horiz))
    return yaw, pitch


def try_shot(rcon, pitch_offset: float) -> dict[str, object]:
    summon_bot(rcon)
    spawn_crystal(rcon)
    command(rcon, "kill @e[type=arrow]", delay=0.0)
    eye = (BASE[0] + 0.5, BASE[1] + 1.62, BASE[2] + 0.5)
    yaw, pitch = yaw_pitch_to(eye, CRYSTAL)
    pitch += pitch_offset
    command(rcon, f"player {BOT} look {yaw:.3f} {pitch:.3f}", delay=0.1)
    command(rcon, f"player {BOT} use continuous", delay=1.35)
    command(rcon, f"player {BOT} stop", delay=0.2)
    arrow = wait_arrow(rcon)
    time.sleep(1.2)
    crystal_alive = entity_exists(rcon, "@e[type=end_crystal,limit=1]")
    return {
        "pitch_offset": pitch_offset,
        "yaw": round(yaw, 3),
        "pitch": round(pitch, 3),
        "arrow_pos": tuple(round(v, 3) for v in arrow),
        "crystal_alive": crystal_alive,
    }


def main() -> int:
    with RconClient(RconConfig()) as rcon:
        setup_world(rcon)
        rows = []
        for offset in [-10.0, -5.0, -2.0, 0.0, 2.0, 5.0]:
            rows.append(try_shot(rcon, offset))
        print(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
