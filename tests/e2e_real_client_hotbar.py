"""Held-item check with a full hotbar, in a disposable client world.

Track P of eval-20261019 lost every bucket and fire use and 114 meals: the
item was brought into hotbar slot 0 and Baritone put the pickaxe back there
before the use. This puts a pickaxe in slot 0, fills the rest of the hotbar,
keeps the items to use in the main inventory and calls the production tools.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Region
from minebot.game.governance import GovernancePolicy
from minebot.game.rcon import RconClient, RconConfig
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc

X, Y, Z = 700, 150, 0  # platform top is Y - 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--ipc-port", type=int, required=True)
    parser.add_argument("--rcon-port", type=int, required=True)
    parser.add_argument("--server-dir", required=True, help="holds server.properties with the rcon password")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    props = dict(line.split("=", 1) for line in Path(args.server_dir, "server.properties").read_text().splitlines()
                 if "=" in line and not line.startswith("#"))
    rcon = RconClient(RconConfig(port=args.rcon_port, password=props["rcon.password"]))
    body = RealClientBody(args.bot, RealClientIpc(port=args.ipc_port, timeout_s=30))
    region = Region("hotbar-fixture", (X - 32, Y - 16, Z - 32), (X + 32, Y + 16, Z + 32))
    registry = build_phase1_registry(body, Phase1RuntimeConfig(region, body_provider="real_client",
        governance_policy=GovernancePolicy(natural_regions=[region], bot_name=args.bot)))
    report: dict = {"records": [], "model_calls": 0,
                    "conditions": "disposable local world; RCON-built platform and inventory; production tool calls"}
    bot = args.bot

    def command(text: str) -> str:
        return rcon.command(text)

    def check(text: str) -> bool:
        return command(text).startswith("Test passed")

    def full_hotbar() -> None:
        command(f"clear {bot}")
        command(f"item replace entity {bot} hotbar.0 with minecraft:diamond_pickaxe")
        for slot, item in enumerate(["stone_sword", "torch 16", "dirt 32", "stone_axe", "bread 4",
                                     "oak_planks 16", "stone_shovel"], start=1):
            command(f"item replace entity {bot} hotbar.{slot} with minecraft:{item}")
        command(f"item replace entity {bot} hotbar.8 with minecraft:cobblestone 64")
        command(f"item replace entity {bot} weapon.offhand with minecraft:shield")
        for item in ("bucket", "water_bucket", "flint_and_steel", "apple 8"):
            command(f"give {bot} minecraft:{item}")
        command(f"tp {bot} {X + 3}.5 {Y} {Z + 4}.5 -90 30")
        time.sleep(1.5)

    def call(case: str, name: str, params: dict, after: str | None = None) -> None:
        started = time.monotonic()
        result = registry.get(name).callable(params)
        metrics = result.metrics or {}
        held = {k: (metrics.get(k) or {}).get("metrics", {}).get("selected_item") for k in ("look", "use")}
        record = {"case": case, "tool": name, "params": params, "success": result.success, "reason": result.reason,
                  "held_at": held, "elapsed_s": round(time.monotonic() - started, 2),
                  "server_check": None if after is None else {"command": after, "passed": check(after)},
                  "result": result.to_payload()}
        report["records"].append(record)
        print(case, name, result.success, result.reason, "held:", held,
              "" if after is None else f"server_check={record['server_check']['passed']}", flush=True)

    command(f"forceload add {X - 16} {Z - 16} {X + 16} {Z + 16}")
    command("gamerule doMobSpawning false")
    command("gamerule doDaylightCycle false")
    command("time set day")
    command(f"fill {X - 6} {Y - 2} {Z - 6} {X + 14} {Y + 6} {Z + 12} minecraft:air")
    command(f"fill {X - 6} {Y - 2} {Z - 6} {X + 14} {Y - 1} {Z + 12} minecraft:stone")
    command(f"setblock {X + 5} {Y - 1} {Z + 4} minecraft:water")
    command(f"setblock {X + 7} {Y - 1} {Z + 4} minecraft:lava")
    time.sleep(1.0)

    full_hotbar()
    call("pickup_water", "use_on_block", {"item": "bucket", "pos": [X + 5, Y - 1, Z + 4], "watched_items": ["bucket", "water_bucket"],
         "required_watched_item_deltas": {"water_bucket": 1}, "timeout_s": 20},
         after=f"execute if block {X + 5} {Y - 1} {Z + 4} minecraft:air")

    # The cell right above a lava source in the floor has no solid neighbour
    # to click: refused with the neighbours named.
    full_hotbar()
    call("pour_above_lava_unsupported", "use_on_block", {"item": "water_bucket", "pos": [X + 7, Y, Z + 4], "timeout_s": 20})
    # Poured on the rim beside it instead, the water flows over the source and
    # turns it into obsidian.
    call("pour_on_rim", "use_on_block", {"item": "water_bucket", "pos": [X + 6, Y, Z + 4], "timeout_s": 20},
         after=f"execute if block {X + 7} {Y - 1} {Z + 4} minecraft:obsidian")
    call("take_water_back", "use_on_block", {"item": "bucket", "pos": [X + 6, Y, Z + 4], "watched_items": ["bucket", "water_bucket"],
         "required_watched_item_deltas": {"water_bucket": 1}, "timeout_s": 20})

    # The obsidian made from the floor source is mined with the diamond
    # pickaxe and picked up.
    full_hotbar()
    call("mine_obsidian", "mine_block_collect", {"pos": [X + 7, Y - 1, Z + 4], "expected_drops": ["obsidian"], "target_block_types": ["obsidian"]},
         after=f"execute if items entity {bot} container.* minecraft:obsidian")

    # AltoClef's cast: lava into a walled open cell, then water above it.
    command(f"fill {X + 9} {Y} {Z + 3} {X + 11} {Y} {Z + 5} minecraft:stone")
    command(f"setblock {X + 10} {Y} {Z + 4} minecraft:air")
    command(f"setblock {X + 11} {Y + 1} {Z + 4} minecraft:stone")  # the cast's tenth block, beside the water cell
    full_hotbar()
    command(f"give {bot} minecraft:lava_bucket")
    call("cast_lava", "use_on_block", {"item": "lava_bucket", "pos": [X + 10, Y, Z + 4], "timeout_s": 20},
         after=f"execute if block {X + 10} {Y} {Z + 4} minecraft:lava")
    call("cast_water", "use_on_block", {"item": "water_bucket", "pos": [X + 10, Y + 1, Z + 4], "timeout_s": 20},
         after=f"execute if block {X + 10} {Y} {Z + 4} minecraft:obsidian")

    # A staged obsidian frame lit on its bottom block, without observe_pos.
    fx, fz = X + 2, Z + 9
    for dx in (1, 2):
        command(f"setblock {fx + dx} {Y - 1} {fz} minecraft:obsidian")
        command(f"setblock {fx + dx} {Y + 3} {fz} minecraft:obsidian")
    for dy in range(0, 3):
        command(f"setblock {fx} {Y + dy} {fz} minecraft:obsidian")
        command(f"setblock {fx + 3} {Y + dy} {fz} minecraft:obsidian")
    full_hotbar()
    command(f"tp {bot} {fx + 1}.5 {Y} {fz - 2}.5 0 30")
    time.sleep(1.0)
    call("light_portal", "use_on_block", {"item": "flint_and_steel", "pos": [fx + 1, Y - 1, fz], "timeout_s": 20},
         after=f"execute if block {fx + 1} {Y} {fz} minecraft:nether_portal")

    full_hotbar()
    command(f"effect give {bot} minecraft:hunger 4 200")
    time.sleep(4.5)
    report["food_before_eat"] = command(f"data get entity {bot} foodLevel")
    call("eat", "consume_item", {"item": "apple"})
    report["food_after_eat"] = command(f"data get entity {bot} foodLevel")
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    passed = sum(1 for r in report["records"] if r["success"])
    print(f"passed {passed}/{len(report['records'])}; report {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
