"""Read player completion counters from a Minecraft world/stats/<uuid>.json."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_stats(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    stats = data.get("stats", data)
    custom = stats.get("minecraft:custom", {})
    killed = stats.get("minecraft:killed", {})
    picked = stats.get("minecraft:picked_up", {})
    crafted = stats.get("minecraft:crafted", {})
    used = stats.get("minecraft:used", {})
    return {
        "minecraft:killed/minecraft:ender_dragon": int(killed.get("minecraft:ender_dragon", 0)),
        "minecraft:custom/minecraft:deaths": int(custom.get("minecraft:deaths", 0)),
        "minecraft:custom/minecraft:play_time": int(custom.get("minecraft:play_time", 0)),
        "minecraft:custom/minecraft:mob_kills": int(custom.get("minecraft:mob_kills", 0)),
        "minecraft:picked_up/minecraft:ender_eye": int(picked.get("minecraft:ender_eye", 0)),
        "minecraft:crafted/minecraft:ender_eye": int(crafted.get("minecraft:ender_eye", 0)),
        "minecraft:used/minecraft:ender_eye": int(used.get("minecraft:ender_eye", 0)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stats", type=Path, help="world/stats/<uuid>.json")
    args = parser.parse_args()
    for key, value in read_stats(args.stats).items():
        print(f"{key}\t{value}")


if __name__ == "__main__":
    main()
