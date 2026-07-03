#!/usr/bin/env python3
"""Find W1 candidate centers with locate-biome plus w1_probe."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from minebot.game.rcon import RconClient, RconConfig

BIOMES = ("minecraft:forest", "minecraft:birch_forest", "minecraft:old_growth_spruce_taiga", "minecraft:taiga")


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate and probe W1 forest candidates.")
    parser.add_argument("--grid", default="-384,-192,0,192,384", help="comma-separated x/z probe grid")
    parser.add_argument("--spawn-y", type=int, default=80)
    parser.add_argument("--radius", type=int, default=48)
    args = parser.parse_args()

    coords = [int(value) for value in args.grid.split(",") if value.strip()]
    centers: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    with RconClient(_rcon_config_from_env()) as rcon:
        for x in coords:
            for z in coords:
                for biome in BIOMES:
                    raw = rcon.command(f"execute positioned {x} 80 {z} run locate biome {biome}")
                    parsed = _parse_locate(raw)
                    if parsed is None:
                        continue
                    cx, cz = parsed
                    if abs(cx) > 448 or abs(cz) > 448:
                        continue
                    key = (cx, cz)
                    if key not in seen:
                        seen.add(key)
                        centers.append((cx, cz, biome))
    if not centers:
        print("No biome locate candidates inside +/-448.")
        return 2

    cmd = [
        sys.executable,
        str(ROOT / "tools" / "probe_w1.py"),
        "--radius",
        str(args.radius),
        "--spawn-y",
        str(args.spawn_y),
        "--json",
    ]
    for cx, cz, _biome in centers:
        cmd.append(f"--center={cx},{cz}")
    completed = subprocess.run(cmd, cwd=ROOT, check=False, text=True, capture_output=True)
    if completed.stdout:
        rows = json.loads(completed.stdout)
    else:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode or 2
    biome_by_center = {(cx, cz): biome for cx, cz, biome in centers}
    for row in rows:
        row["biome_source"] = biome_by_center.get(tuple(row["center"]), "")
    print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if any(row.get("pass") for row in rows) else 2


def _rcon_config_from_env() -> RconConfig:
    return RconConfig(
        host=os.environ.get("MINEBOT_REAL_RCON_HOST", "127.0.0.1"),
        port=int(os.environ.get("MINEBOT_REAL_RCON_PORT", "25576")),
        password=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"),
        timeout_s=float(os.environ.get("MINEBOT_REAL_RCON_TIMEOUT", "30")),
        reconnect_attempts=2,
    )


def _parse_locate(raw: str) -> tuple[int, int] | None:
    match = re.search(r"\[(-?\d+),\s*-?\d+,\s*(-?\d+)\]", raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


if __name__ == "__main__":
    raise SystemExit(main())
