"""Focused physical regression on an explicitly disposable, separate local client."""
import argparse
import gzip
import json
import os
from pathlib import Path
import struct
import threading
import time

from minebot.app.client_body_tools import client_body_tools
from minebot.contract import Action
from minebot.game.errors import BodyActionTimeoutError
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc
from minebot.game.rcon import RconClient, RconConfig


def schematic(path):
    def tag(kind, name, value):
        name = name.encode()
        return bytes([kind]) + struct.pack(">H", len(name)) + name + value
    palette = tag(3, "minecraft:cobblestone", struct.pack(">i", 0)) + b"\0"
    content = tag(3, "Version", struct.pack(">i", 2)) + tag(3, "DataVersion", struct.pack(">i", 3700))
    for name, count in [("Width", 2), ("Height", 1), ("Length", 2)]:
        content += tag(2, name, struct.pack(">h", count))
    content += tag(11, "Offset", struct.pack(">iiii", 3, 0, 0, 0))
    content += tag(3, "PaletteMax", struct.pack(">i", 1)) + tag(10, "Palette", palette)
    content += tag(7, "BlockData", struct.pack(">i", 4) + b"\0" * 4) + b"\0"
    path.write_bytes(gzip.compress(tag(10, "Schematic", content)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--ipc-port", type=int, required=True)
    parser.add_argument("--rcon-port", type=int, required=True)
    parser.add_argument("--player", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    body = RealClientBody(args.player, RealClientIpc(port=args.ipc_port, timeout_s=10))
    rcon = RconClient(RconConfig(port=args.rcon_port, password=os.environ.get("MINEBOT_TEST_RCON_PASSWORD", "test")))
    tools = {tool.name: tool for tool in client_body_tools(body)}
    report = {"conditions": "Separate disposable world; supplied floor/materials, teleport and frozen movement attribute; no LLM or active-house intervention.", "cases": []}

    def command(text):
        return rcon.command(text)

    def save():
        (root / "results.json").write_text(json.dumps(report, indent=2) + "\n")

    def fixture():
        command(f"attribute {args.player} minecraft:movement_speed base set 0.1")
        command("forceload add 0 0 64 16")
        command("fill 0 99 0 64 99 16 stone")
        command("fill 0 100 0 64 106 16 air")
        command(f"clear {args.player}")
        command(f"effect clear {args.player}")
        command(f"tp {args.player} 1.5 100 1.5")
        body.ipc.baritone_command("set allowInventory false")
        body.ipc.baritone_command("set allowSprint true")
        time.sleep(0.4)

    def run(name, action, cancel_after=None):
        row = {"name": name, "action": action.name, "params": action.params, "samples": []}
        report["cases"].append(row)
        accepted = body.execute(action)
        assert accepted.ok and accepted.accepted, accepted
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            state = body.ipc.state()
            row["samples"].append({"t": round(elapsed, 3), **{key: state.get(key) for key in (
                "position", "pathing", "any_process_active", "builder_active", "allow_inventory", "navigation")}})
            if cancel_after is not None and elapsed >= cancel_after:
                body.interrupt("developer_cancel")
                cancel_after = None
            if not state.get("any_process_active") and not state.get("pathing"):
                try:
                    terminal = body.await_action_terminal(action.id, timeout_s=0.2)
                    break
                except BodyActionTimeoutError:
                    pass
            assert elapsed < 65, row
            time.sleep(0.1)
        row.update(elapsed_s=round(time.monotonic() - started, 3), terminal=terminal.data)
        row["after"] = body.perceive("baritoneState", {}).data
        save()
        print(name, terminal.data.get("status"), terminal.data.get("reason"), row["elapsed_s"], flush=True)
        return row

    validated = False
    try:
        body.get_state()
        initial = body.ipc.state()
        assert initial.get("disposable_world") is True
        assert initial.get("player") == args.player
        validated = True
        fixture()
        normal = run("continuous_flat_travel", Action.create("navigate", {
            "goal": {"kind": "block", "pos": [51, 100, 1]}, "timeout_s": 30}))
        assert normal["terminal"]["success"]

        fixture()
        command(f"attribute {args.player} minecraft:movement_speed base set 0")
        stuck = run("two_failed_movements", Action.create("navigate", {
            "goal": {"kind": "block", "pos": [10, 100, 1]}, "timeout_s": 60}))
        assert stuck["terminal"]["reason"] == "no_progress"
        nav = stuck["terminal"]["navigation"]
        assert nav["repeated_failures"] == 2
        assert nav["movement"] == "MovementTraverse" and nav["src"] != nav["dest"]
        assert nav["path_position"] < nav["path_movements"]
        assert stuck["elapsed_s"] < 20 and not stuck["after"]["any_process_active"]

        fixture()
        command("setblock 2 100 1 cobblestone")
        command(f"item replace entity {args.player} hotbar.0 with wooden_pickaxe")
        command(f"effect give {args.player} minecraft:mining_fatigue 60 1 true")
        time.sleep(0.4)
        mining = run("stationary_slow_mining", Action.create("clearArea", {
            "start": [2, 100, 1], "end": [2, 100, 1], "timeout_s": 60}))
        assert mining["terminal"]["success"] and mining["elapsed_s"] > 5

        for material, size in [("cobblestone", [2, 1, 2]), ("oak_slab", [3, 1, 1])]:
            fixture()
            command(f"item replace entity {args.player} inventory.13 with {material} 48")
            row = run("main_inventory_" + material, Action.create("buildStructure", {
                "anchor": [5, 100, 5], "size": size, "block": material, "timeout_s": 30}))
            assert row["terminal"]["success"] and not row["after"]["allow_inventory"]

        fixture()
        body.ipc.baritone_command("set allowInventory true")
        command(f"item replace entity {args.player} inventory.13 with cobblestone 48")
        preserved = run("preserve_enabled_inventory", Action.create("buildStructure", {
            "anchor": [5, 100, 5], "size": [2, 1, 2], "block": "cobblestone", "timeout_s": 30}))
        assert preserved["terminal"]["success"] and preserved["after"]["allow_inventory"]

        fixture()
        command(f"item replace entity {args.player} inventory.13 with cobblestone 64")
        path = root / "fixture.schem"
        schematic(path)
        result = tools["build_schematic"].callable({"path": str(path), "origin": [5, 100, 5], "timeout_s": 30})
        report["cases"].append({"name": "tracked_schematic_tool", "result": result.to_payload()})
        assert result.success, result.to_payload()
        assert not body.ipc.state()["allow_inventory"]
        save()

        for kind, params in [("buildStructure", {"anchor": [5, 100, 5], "size": [8, 1, 8], "block": "cobblestone"}),
                             ("builderBuildFile", {"path": str(path), "origin": [30, 100, 5]})]:
            fixture()
            command(f"item replace entity {args.player} inventory.13 with cobblestone 64")
            row = run("cancel_" + kind, Action.create(kind, {**params, "timeout_s": 60}), cancel_after=0.2)
            assert row["terminal"]["status"] == "canceled"
            assert not row["after"]["allow_inventory"] and not row["after"]["any_process_active"]

        fixture()
        failure = run("builder_missing_material", Action.create("buildStructure", {
            "anchor": [5, 100, 5], "size": [2, 1, 2], "block": "cobblestone", "timeout_s": 2}))
        assert not failure["terminal"]["success"] and not failure["after"]["allow_inventory"]
        fixture()
        rejected = run("builder_file_rejected", Action.create("builderBuildFile", {
            "path": str(root / "missing.schem"), "origin": [5, 100, 5], "timeout_s": 10}))
        assert not rejected["terminal"]["success"] and not rejected["after"]["allow_inventory"]
        report["success"] = True
        save()
    finally:
        if validated:
            command(f"attribute {args.player} minecraft:movement_speed base set 0.1")
            body.interrupt("probe_cleanup")
        body.ipc.close()
        save()


if __name__ == "__main__":
    main()
