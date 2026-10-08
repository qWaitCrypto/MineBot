"""Directed Body proof on an explicitly disposable loopback server.

Run with two launched clients for handoff; fixture setup uses local RCON only.
This is an opt-in experiment, not part of the normal unit-test suite.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import threading
import traceback

from minebot.contract import Action
from minebot.contract import Region
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.game.governance import GovernancePolicy
from minebot.game.rcon import RconClient, RconConfig
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc


TASKS = {
    "collection": "finish small trees; demand-based stone/iron/giant tree and cancel",
    "navigation": "cross an obstacle, collect logs, and confirm arrival/inventory",
    "building": "build four blocks and clear them; check every server block",
    "farming": "harvest ripe wheat through Baritone and confirm inventory",
    "controls": "cancel navigation and exploration; verify old controls stop",
    "interactions": "use food, place a block, open a gate, and operate a lever",
    "consumption": "eat bread, confirm hunger/inventory, and cancel before eating completes",
    "flight": "fly 128 blocks with Elytra, land at the destination, and verify server position",
    "vertical": "dig down, pillar up, return to surface; bounded refusals and cancel",
    "vertical_denied": "refuse vertical mutation without disposable-world permission",
    "vertical_tools": "Agent tool down/up/surface with chosen scaffold and cancel",
    "handoff": "give two diamonds to a real recipient; full-inventory refusal",
    "handoff_tools": "Agent give_player tool with server recipient/count verification",
    "ranged": "bow/crossbow damage, missing ammunition and charging cancellation",
    "menus": "craft, chest transfer, smelt/reclaim and inventory-slot regressions",
    "combat_tools": "Agent melee and bow/crossbow against a named target, decoy intact",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--case", choices=TASKS, required=True)
    parser.add_argument("--client-name", default="MineBotCombat")
    parser.add_argument("--receiver-name", default="Receiver")
    parser.add_argument("--output", type=Path, help="result JSON (default: timestamped file under logs/)")
    args = parser.parse_args()
    rcon = RconClient(RconConfig(password=os.environ.get("MINEBOT_TEST_RCON_PASSWORD", "test")))
    body = RealClientBody(args.client_name, RealClientIpc(timeout_s=30))
    records: list[dict[str, object]] = []
    started = time.monotonic()
    report = {"task": TASKS[args.case], "case": args.case,
              "evidence": "directed_body_task", "success": False,
              "server": "127.0.0.1:25566", "client": args.client_name,
              "tuple": "MC 26.1.2 / Fabric 0.19.3 / API 0.152.1 / Java 25 / Baritone 1.18.0",
              "conditions": "disposable fixture; supplied resources; no planning model",
              "records": records}

    def command(text: str) -> str:
        report["step"] = text
        reply = rcon.command(text)
        if args.case == "collection" and text.startswith(("fill ", "forceload ", "setblock ", "give ", "tp ", "gamemode ")):
            report.setdefault("fixture_commands", []).append({"command": text, "reply": reply})
            assert not any(error in reply for error in ("not loaded", "Incorrect argument", "Unknown or incomplete")), reply
        return reply

    def wait_at(x: float, y: float, z: float) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pos = body.get_state().pos
            if pos and sum((a - b) ** 2 for a, b in zip(pos, (x, y, z))) < 0.02:
                time.sleep(0.3)
                return
            time.sleep(0.1)
        raise AssertionError("client did not observe the fixture teleport")

    def wait_block(pos: list[int], block: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = body.perceive("blockAt", dict(zip(("x", "y", "z"), pos))).data
            if observed.get("type") == f"minecraft:{block}":
                return
            time.sleep(0.1)
        raise AssertionError({"reason": "fixture block not observed", "pos": pos, "observed": observed})

    def action(name: str, params: dict[str, object], *, cancel: bool = False) -> dict[str, object]:
        report["step"] = {"action": name, "params": params}
        request = Action.create(name, {"timeout_s": 30, **params})
        record = {"action": name, "params": params, "action_id": request.id}
        records.append(record)
        accepted = body.execute(request)
        assert accepted.ok and accepted.accepted, accepted
        if cancel:
            time.sleep(0.4)
            assert body.interrupt("fixture cancellation").ok
        event = body.await_action_terminal(request.id, timeout_s=40)
        data = dict(event.data)
        record["terminal"] = data
        print(name, data["status"], data["reason"], data.get("elapsed_ms"), flush=True)
        return data

    def tool(name: str, params: dict[str, object]) -> object:
        report["step"] = {"tool": name, "params": params}
        region = Region("disposable-task", (-256, 0, -256), (256, 320, 256))
        registry = build_phase1_registry(body, Phase1RuntimeConfig(
            region, body_provider="real_client",
            governance_policy=GovernancePolicy(natural_regions=[region], bot_name=args.client_name)))
        if name == "collect_resource":
            from minebot.brain.composition import CompositionBudget, CompositionContext, collect_resource_tool
            from minebot.brain.lifecycle import LifecycleState
            from minebot.brain.modes import ModeRuntime
            from minebot.brain.progress import ProgressAuthority
            from minebot.brain.registry import WeldContext
            registry.register(collect_resource_tool(CompositionContext(
                registry=registry,
                weld_context=WeldContext(body=body, authority=ProgressAuthority(), goal_text="collect fixture resources"),
                runtime_profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
                budget=CompositionBudget(max_candidates=96, max_mutating_calls=96, max_wall_s=300))))
        result = registry.get(name).callable(params)
        records.append({"tool": name, "params": params, "result": result.to_payload()})
        print(name, result.success, result.reason, flush=True)
        assert result.success, result.to_payload()
        return result

    def flat_fixture(x: int) -> None:
        command(f"fill {x} 149 -4 {x + 24} 149 6 minecraft:stone")
        command(f"fill {x} 150 -4 {x + 24} 155 6 minecraft:air")
        command(f"gamemode survival {args.client_name}")
        command(f"effect clear {args.client_name}")
        command(f"clear {args.client_name}")
        command(f"tp {args.client_name} {x + 0.5} 150 0.5")
        wait_at(x + 0.5, 150, 0.5)

    def check_server(command_text: str) -> None:
        observed = command(command_text)
        records[-1].setdefault("server_checks", []).append({"command": command_text, "observed": observed})
        assert observed.startswith("Test passed"), {"command": command_text, "observed": observed}

    def setup_vertical() -> None:
        # Enclosed shaft in a stone cube, high above the disposable terrain.
        command("fill 60 147 -4 68 154 4 minecraft:stone")
        command("fill 60 155 -4 68 164 4 minecraft:air")
        command("fill 64 150 0 64 151 0 minecraft:air")
        command(f"gamemode survival {args.client_name}")
        command(f"clear {args.client_name}")
        command(f"give {args.client_name} minecraft:diamond_pickaxe")
        command(f"give {args.client_name} minecraft:cobblestone 32")
        command(f"tp {args.client_name} 64.5 150 0.5")
        wait_at(64.5, 150, 0.5)

    def verify_height(data: dict[str, object], height: int) -> None:
        assert data["success"] and data["on_ground"], data
        time.sleep(0.3)
        pos = body.get_state().pos
        assert pos and abs(pos[1] - height) < 0.1, pos
        actual = command(f"data get entity {args.client_name} Pos")
        records[-1]["server_position"] = actual
        assert f", {height}.0d," in actual, actual

    try:
        body.get_state()
        report["initial_state"] = body.ipc.state()
        report["seed"] = command("seed")
        report["difficulty"] = command("difficulty")
        if args.case == "collection":
            # Preserve existing inventory. This is an isolated supplied fixture,
            # not a from-zero model task.
            assert report["initial_state"]["disposable_world"] is True
            command("forceload add 208 -6 236 8")
            time.sleep(1)
            command("fill 208 60 -6 236 69 8 minecraft:dirt")
            command("fill 208 70 -6 236 100 8 minecraft:air")
            command(f"gamemode survival {args.client_name}")
            command(f"tp {args.client_name} 208.5 70 0.5")
            wait_at(208.5, 70, 0.5)
            for item in ("iron_axe", "iron_pickaxe"):
                command(f"give {args.client_name} minecraft:{item}")
            command(f"give {args.client_name} minecraft:dirt 64")
            command("kill @e[type=minecraft:item,x=208,y=70,z=-6,dx=28,dy=30,dz=14]")
            first = [[212, y, 0] for y in range(70, 76)] + [[213, 74, 0], [214, 75, 0]]
            second = [[221, y, 0] for y in range(70, 78)]
            third = [[230, y, 0] for y in range(70, 76)]
            for pos in first + second + third:
                command(f"setblock {' '.join(map(str, pos))} minecraft:oak_log")
            before = body.get_state().inventory_counts.get("oak_log", 0)
            tool("collect_resource", {"item": "oak_log", "count": before + 9})
            assert body.get_state().inventory_counts.get("oak_log", 0) >= before + 16
            for pos in first + second:
                check_server(f"execute if block {' '.join(map(str, pos))} minecraft:air")
            for pos in third:
                check_server(f"execute if block {' '.join(map(str, pos))} minecraft:oak_log")
            records[-1]["server_inventory"] = command(f"data get entity {args.client_name} Inventory")
            command("fill 212 70 3 219 72 5 minecraft:stone")
            before = body.get_state().inventory_counts.get("cobblestone", 0)
            tool("collect_resource", {"item": "cobblestone", "count": before + 40})
            assert body.get_state().inventory_counts.get("cobblestone", 0) >= before + 40
            records[-1]["server_inventory"] = command(f"data get entity {args.client_name} Inventory")
            # The task ends with a real inventory result, without clearing all
            # 72 connected stone cells just because they touch each other.
            remaining_stone = sum("Test passed" in command(f"execute if block {x} {y} {z} minecraft:stone")
                                  for x in range(212, 220) for y in range(70, 73) for z in range(3, 6))
            records[-1]["remaining_stone"] = remaining_stone
            assert remaining_stone > 0, remaining_stone
            command("fill 212 70 -2 215 72 -2 minecraft:iron_ore")
            before = body.get_state().inventory_counts.get("raw_iron", 0)
            tool("collect_resource", {"item": "raw_iron", "count": before + 12})
            assert body.get_state().inventory_counts.get("raw_iron", 0) >= before + 12
            records[-1]["server_inventory"] = command(f"data get entity {args.client_name} Inventory")
            command("fill 212 70 -5 213 89 -4 minecraft:jungle_log")
            before = body.get_state().inventory_counts.get("jungle_log", 0)
            tool("collect_resource", {"item": "jungle_log", "count": before + 8})
            assert body.get_state().inventory_counts.get("jungle_log", 0) >= before + 8
            check_server("execute if block 212 89 -5 minecraft:jungle_log")
            records[-1]["server_inventory"] = command(f"data get entity {args.client_name} Inventory")
            targets = [[230, y, 0] for y in range(70, 76)]
            canceled = action("collectBlocks", {"targets": targets, "expected_block_ids": ["oak_log"],
                              "expected_item_ids": ["oak_log"]}, cancel=True)
            assert canceled["status"] == "canceled", canceled
            assert not body.ipc.state()["any_process_active"]
        elif args.case == "navigation":
            flat_fixture(128)
            command("fill 135 150 -1 135 152 1 minecraft:stone")
            command("setblock 143 150 0 minecraft:oak_log")
            command(f"give {args.client_name} minecraft:iron_axe")
            tool("move_to", {"pos": [140, 150, 0], "timeout_s": 30})
            check_server(f"execute if entity @a[name={args.client_name},x=140,y=150,z=0,distance=..2]")
            data = action("mineResource", {"block": "oak_log", "quantity": 1})
            assert data["success"], data
            check_server(f'execute if data entity {args.client_name} Inventory[{{id:"minecraft:oak_log"}}]')
            check_server("execute if block 143 150 0 minecraft:air")
        elif args.case == "building":
            flat_fixture(160)
            command(f"give {args.client_name} minecraft:cobblestone 16")
            command(f"give {args.client_name} minecraft:diamond_pickaxe")
            data = action("buildStructure", {"origin": [164, 150, 0], "size": [2, 1, 2], "block": "cobblestone"})
            assert data["success"], data
            for x in (164, 165):
                for z in (0, 1):
                    check_server(f"execute if block {x} 150 {z} minecraft:cobblestone")
            data = action("clearArea", {"start": [164, 150, 0], "end": [165, 150, 1]})
            assert data["success"], data
            for x in (164, 165):
                for z in (0, 1):
                    check_server(f"execute if block {x} 150 {z} minecraft:air")
        elif args.case == "farming":
            flat_fixture(192)
            command("fill 195 149 0 198 149 0 minecraft:farmland[moisture=7]")
            command("fill 195 150 0 198 150 0 minecraft:wheat[age=7]")
            command("setblock 196 149 1 minecraft:water")
            data = action("farmArea", {"anchor": [196, 150, 0], "range": 8})
            assert data["success"], data
            check_server(f'execute if data entity {args.client_name} Inventory[{{id:"minecraft:wheat"}}]')
        elif args.case == "controls":
            flat_fixture(128)
            for name, params in (("navigate", {"goal": {"kind": "block", "x": 150, "y": 150, "z": 0}}),
                                 ("explore", {"anchor": [128, 150, 0]})):
                data = action(name, params, cancel=True)
                assert data["status"] == "canceled", data
                time.sleep(0.5)
                settled = body.ipc.state()
                records[-1]["settled_state"] = settled
                assert not settled["any_process_active"] and not settled["pathing"], settled
        elif args.case == "flight":
            # A drop-start fixture, with supplied equipment. After the action
            # begins, only Body/Baritone controls flight and landing.
            command("forceload add 304 0 480 64")
            command("fill 424 160 8 472 160 56 minecraft:stone")
            command("fill 424 161 8 472 173 56 minecraft:air")
            command(f"gamemode survival {args.client_name}")
            command(f"effect clear {args.client_name}")
            command(f"clear {args.client_name}")
            command(f"item replace entity {args.client_name} armor.chest with minecraft:elytra")
            command(f"give {args.client_name} minecraft:firework_rocket 128")
            body.ipc.control("settings", allow_break=False, allow_place=False)
            deadline = time.monotonic() + 5
            while body.ipc.state().get("chest_item") != "elytra":
                assert time.monotonic() < deadline, "client did not observe Elytra equipment"
                time.sleep(0.1)
            report["conditions"] = "disposable overworld; drop start at (320.5,220,32.5); supplied Elytra/128 rockets; no intervention after request"
            request = Action.create("elytraTravel", {"target": [448, 161, 32], "timeout_s": 120})
            record = {"action": request.name, "params": request.params, "action_id": request.id, "flight_samples": []}
            records.append(record)
            command(f"tp {args.client_name} 320.5 220 32.5")
            deadline = time.monotonic() + 3
            while True:
                launch = body.ipc.state()
                if abs(launch["position"][0] - 320.5) < 0.1 and not launch["on_ground"]:
                    record["launch_state"] = launch
                    break
                assert time.monotonic() < deadline, "client did not observe drop-start fixture"
                time.sleep(0.02)
            accepted = body.execute(request)
            assert accepted.accepted, accepted
            terminal = []
            def await_flight() -> None:
                try:
                    terminal.append(dict(body.await_action_terminal(request.id, timeout_s=130).data))
                except Exception as error:
                    terminal.append({"success": False, "reason": str(error)})
            worker = threading.Thread(target=await_flight)
            worker.start()
            while worker.is_alive():
                state = body.ipc.state()
                sample = {"position": state.get("position"), "fall_flying": state.get("fall_flying"),
                          "server_position": command(f"data get entity {args.client_name} Pos"),
                          "server_flying": command(f"data get entity {args.client_name} FallFlying")}
                record["flight_samples"].append(sample)
                time.sleep(0.25)
            record["terminal"] = terminal[0]
            assert terminal[0]["success"], terminal[0]
            assert any(s["server_flying"].endswith(": 1b") for s in record["flight_samples"]), "server never observed flight"
            check_server(f"execute if entity @a[name={args.client_name},x=448.5,y=161,z=32.5,distance=..8]")
            assert body.ipc.state()["on_ground"], "client did not land"
        elif args.case == "consumption":
            flat_fixture(224)
            command(f"give {args.client_name} minecraft:bread 2")
            command(f"effect give {args.client_name} minecraft:hunger 10 255 true")
            deadline = time.monotonic() + 5
            while body.get_state().food >= 18:
                assert time.monotonic() < deadline, "fixture did not make player hungry"
                time.sleep(0.1)
            command(f"effect clear {args.client_name}")
            food_before = body.get_state().food
            tool("consume_item", {"item": "bread"})
            check_server(f'execute if data entity {args.client_name} Inventory[{{id:"minecraft:bread",count:1}}]')
            observed = command(f"data get entity {args.client_name} foodLevel")
            records[-1]["server_food"] = observed
            assert int(observed.rsplit(": ", 1)[-1]) > food_before, observed
            tool("consume_item", {"item": "bread"})  # Already full; preserve remaining bread.
            check_server(f'execute if data entity {args.client_name} Inventory[{{id:"minecraft:bread",count:1}}]')
            command(f"effect give {args.client_name} minecraft:hunger 10 255 true")
            deadline = time.monotonic() + 5
            while body.get_state().food >= 18:
                assert time.monotonic() < deadline, "fixture did not make player hungry"
                time.sleep(0.1)
            command(f"effect clear {args.client_name}")
            data = action("useItem", {"mode": "continuous", "ticks": 80, "item": "bread"}, cancel=True)
            assert data["status"] == "canceled", data
            time.sleep(2)
            check_server(f'execute if data entity {args.client_name} Inventory[{{id:"minecraft:bread",count:1}}]')
            settled = body.ipc.state()
            records[-1]["settled_state"] = settled
            assert not settled["using_item"] and not settled["use_key_down"], settled
        elif args.case == "interactions":
            flat_fixture(224)
            command(f"give {args.client_name} minecraft:cobblestone 4")
            command(f"give {args.client_name} minecraft:bread 2")
            command(f"effect give {args.client_name} minecraft:hunger 3 100 true")
            deadline = time.monotonic() + 5
            while body.get_state().food >= 18:
                if time.monotonic() >= deadline:
                    raise AssertionError("fixture did not make player hungry")
                time.sleep(0.1)
            command(f"effect clear {args.client_name}")
            food_before = body.get_state().food
            tool("consume_item", {"item": "bread"})
            assert body.get_state().food > food_before, "eating did not increase hunger points"
            tool("place_block", {"pos": [226, 150, 0], "block_type": "cobblestone"})
            check_server("execute if block 226 150 0 minecraft:cobblestone")
            command("setblock 226 150 2 minecraft:oak_fence_gate")
            wait_block([226, 150, 2], "oak_fence_gate")
            tool("set_openable_state", {"pos": [226, 150, 2], "open": True})
            check_server("execute if block 226 150 2 minecraft:oak_fence_gate[open=true]")
            command("setblock 226 150 -2 minecraft:lever[face=floor]")
            wait_block([226, 150, -2], "lever")
            tool("set_switch_state", {"pos": [226, 150, -2], "powered": True})
            check_server("execute if block 226 150 -2 minecraft:lever[powered=true]")
        elif args.case == "vertical_denied":
            assert not body.ipc.state().get("disposable_world"), "launch without disposable-world opt-in"
            setup_vertical()
            data = action("digDown", {"target_y": 149, "max_distance": 8, "max_mutations": 16})
            assert data["reason"] == "vertical_mutation_not_authorized" and data["mutations"] == 0, data
            assert command("execute if block 64 149 0 minecraft:stone") == "Test passed"
            assert body.get_state().pos == (64.5, 150.0, 0.5)
        elif args.case == "vertical":
            assert body.ipc.state().get("disposable_world"), "launch with MINEBOT_CLIENT_DISPOSABLE_WORLD=true"
            setup_vertical()
            data = action("digDown", {"target_y": 149, "max_distance": 8, "max_mutations": 16})
            verify_height(data, 149)
            floor = command("execute if block 64 149 0 minecraft:air")
            records[-1]["server_floor"] = floor
            assert floor == "Test passed", floor

            setup_vertical()
            data = action("digUp", {"target_y": 152, "max_distance": 8, "max_mutations": 16})
            verify_height(data, 152)
            data = action("ascend", {"max_distance": 8, "max_mutations": 16})
            verify_height(data, 155)

            setup_vertical()
            data = action("digDown", {"target_y": 140, "max_distance": 4, "max_mutations": 16})
            assert data["reason"] == "vertical_target_out_of_bounds" and data["mutations"] == 0, data
            data = action("digDown", {"target_y": 149, "max_distance": 4, "max_mutations": 0})
            assert data["reason"] == "vertical_mutation_budget_exceeded" and data["mutations"] == 0, data
            assert command("execute if block 64 149 0 minecraft:stone") == "Test passed"

            command("setblock 64 148 0 minecraft:water")
            time.sleep(0.3)
            data = action("digDown", {"target_y": 149, "max_distance": 4, "max_mutations": 16})
            assert data["reason"] == "vertical_liquid_hazard" and data["mutations"] == 0, data
            assert command("execute if block 64 149 0 minecraft:stone") == "Test passed"

            setup_vertical()
            data = action("digUp", {"target_y": 155, "max_distance": 8, "max_mutations": 16}, cancel=True)
            assert data["status"] == "canceled" and not data["any_process_active"] and not data["pathing"], data
            before = body.get_state().pos
            time.sleep(0.5)
            assert not body.ipc.state()["any_process_active"]
            records[-1]["settled_position"] = body.get_state().pos
            records[-1]["position_at_cancel"] = before
        elif args.case == "vertical_tools":
            assert body.ipc.state().get("disposable_world")
            region = Region("disposable-vertical-tools", (60, 147, -4), (68, 165, 4))
            registry = build_phase1_registry(body, Phase1RuntimeConfig(
                region, body_provider="real_client",
                governance_policy=GovernancePolicy(natural_regions=[region], bot_name=args.client_name),
            ))

            def vertical_tool(name: str, params: dict[str, object], height: int | None) -> dict[str, object]:
                started = time.monotonic()
                result = registry.get(name).callable(params)
                records.append({"tool": name, "params": params, "result": result.to_payload(),
                                "elapsed_s": round(time.monotonic() - started, 3)})
                print(name, result.success, result.reason, records[-1]["elapsed_s"], flush=True)
                if height is not None:
                    verify_height(dict(result.metrics or {}), height)
                return result.to_payload()

            setup_vertical()
            vertical_tool("dig_down", {"target_y": 149, "max_steps": 8}, 149)
            assert command("execute if block 64 149 0 minecraft:air") == "Test passed"
            setup_vertical()
            command(f"clear {args.client_name} minecraft:cobblestone")
            command(f"give {args.client_name} minecraft:dirt 32")
            time.sleep(0.3)
            vertical_tool("dig_up", {"target_y": 152, "max_steps": 8, "scaffold_blocks": ["dirt"]}, 152)
            assert command("execute if block 64 150 0 minecraft:dirt") == "Test passed"
            vertical_tool("go_to_surface", {"timeout_s": 15}, 155)
            setup_vertical()
            failed = vertical_tool("dig_down", {"target_y": 140, "max_steps": 4}, None)
            assert not failed["success"] and failed["reason"] == "vertical_target_out_of_bounds"
            assert command("execute if block 64 149 0 minecraft:stone") == "Test passed"
            setup_vertical()
            canceled = []
            worker = threading.Thread(target=lambda: canceled.append(
                registry.get("dig_up").callable({"target_y": 155, "max_steps": 8})))
            worker.start()
            time.sleep(0.4)
            stopped_at = time.monotonic()
            assert body.interrupt("cancel vertical tool fixture").ok
            worker.join(5)
            assert not worker.is_alive() and len(canceled) == 1
            result = canceled[0]
            records.append({"tool": "dig_up", "result": result.to_payload(), "cancel": True,
                            "cancel_latency_ms": round((time.monotonic() - stopped_at) * 1000)})
            assert not result.success and result.metrics["status"] == "canceled", result.to_payload()
            time.sleep(0.5)
            settled = body.ipc.state()
            assert not settled["any_process_active"] and not settled["pathing"]
            print("dig_up canceled", records[-1]["cancel_latency_ms"], "ms", flush=True)
        elif args.case in {"handoff", "handoff_tools"}:
            registry = build_phase1_registry(body, Phase1RuntimeConfig(
                Region("disposable-handoff-tools", (-4, 69, -4), (4, 74, 4)), body_provider="real_client",
            ))

            def handoff(timeout_ticks: int) -> dict[str, object]:
                if args.case == "handoff":
                    return action("handoffItem", {"receiver": args.receiver_name, "item": "diamond", "slot": 0,
                                                  "count": 2, "pickup_timeout_ticks": timeout_ticks})
                started = time.monotonic()
                params = {"player_name": args.receiver_name, "item": "diamond", "count": 2}
                result = registry.get("give_player").callable(params)
                records.append({"tool": "give_player", "params": params, "result": result.to_payload(),
                                "elapsed_s": round(time.monotonic() - started, 3)})
                print("give_player", result.success, result.reason, records[-1]["elapsed_s"], flush=True)
                return {**dict(result.metrics or {}), "success": result.success, "reason": result.reason}

            command(f"gamemode survival {args.receiver_name}")
            command("fill -4 69 -4 4 69 4 minecraft:stone")
            command("fill -4 70 -4 4 73 4 minecraft:air")
            command(f"tp {args.client_name} 0.5 70 0.5")
            command(f"tp {args.receiver_name} 1.5 70 0.5")
            command(f"clear {args.client_name}")
            command(f"clear {args.receiver_name}")
            command("kill @e[type=item,x=0,y=70,z=0,distance=..8]")
            command(f"give {args.client_name} minecraft:diamond 2")
            wait_at(0.5, 70, 0.5)
            data = handoff(100)
            assert data["success"] and data["handoff_receipt"] and data["handoff_picked_up"] == 2, data
            recipient_inventory = command(f"data get entity {args.receiver_name} Inventory")
            records[-1]["server_recipient_inventory"] = recipient_inventory
            assert 'count: 2' in recipient_inventory and 'minecraft:diamond' in recipient_inventory, recipient_inventory
            if args.case == "handoff_tools":
                assert data["pickup_receipt"]["player"] == args.receiver_name and data["pickup_receipt"]["count"] == 2
            assert command("execute if entity @e[type=item,x=0,y=70,z=0,distance=..8]") == "Test failed"

            # A real player with a full inventory cannot receive the drop.
            command(f"clear {args.receiver_name}")
            command(f"give {args.receiver_name} minecraft:cobblestone 2304")
            command(f"give {args.client_name} minecraft:diamond 2")
            time.sleep(0.3)
            data = handoff(20)
            assert not data["success"] and not data["handoff_receipt"] and data["handoff_picked_up"] == 0, data
            assert data["reason"] == "receiver_pickup_unconfirmed", data
            assert "minecraft:diamond" not in command(f"data get entity {args.receiver_name} Inventory")
        elif args.case == "combat_tools":
            command("fill 78 149 -4 100 149 6 minecraft:stone")
            command("fill 78 150 -4 100 154 6 minecraft:air")
            command("time set midnight")
            region = Region("disposable-combat-tools", (78, 149, -4), (100, 155, 6))
            registry = build_phase1_registry(body, Phase1RuntimeConfig(
                region, body_provider="real_client",
                governance_policy=GovernancePolicy(natural_regions=[region], bot_name=args.client_name),
            ))

            def combat_fixture(weapon: str, *, ammunition: bool = True) -> None:
                for name in ("ToolTarget", "ToolDecoy", "RangedFixture"):
                    command(f"kill @e[type=zombie,name={name}]")
                command(f"gamemode survival {args.client_name}")
                command(f"clear {args.client_name}")
                command(f"item replace entity {args.client_name} inventory.18 with minecraft:{weapon}")
                if ammunition and weapon in {"bow", "crossbow"}:
                    command(f"give {args.client_name} minecraft:arrow 16")
                command(f"tp {args.client_name} 80.5 150 0.5")
                command('summon zombie 88.5 150 0.5 {NoAI:1b,Silent:1b,CustomName:"ToolTarget",PersistenceRequired:1b}')
                command('summon zombie 81.5 150 2.5 {NoAI:1b,Silent:1b,CustomName:"ToolDecoy",PersistenceRequired:1b}')
                wait_at(80.5, 150, 0.5)

            def combat_tool(name: str, params: dict[str, object], *, expected_reason: str) -> None:
                started = time.monotonic()
                result = registry.get(name).callable(params)
                record = {"tool": name, "params": params, "result": result.to_payload(),
                          "elapsed_s": round(time.monotonic() - started, 3),
                          "server_target_health": command("data get entity @e[type=zombie,name=ToolTarget,limit=1] Health"),
                          "server_decoy_health": command("data get entity @e[type=zombie,name=ToolDecoy,limit=1] Health")}
                records.append(record)
                print(name, result.success, result.reason, record["elapsed_s"], flush=True)
                assert result.reason == expected_reason, result.to_payload()
                assert result.success == (expected_reason in {"killed", "damage_observed"}), result.to_payload()
                assert record["server_decoy_health"].endswith(": 20.0f"), record
                if expected_reason == "killed":
                    assert command("execute unless entity @e[type=zombie,name=ToolTarget]") == "Test passed"
                elif expected_reason == "damage_observed":
                    assert not record["server_target_health"].endswith(": 20.0f"), record

            combat_fixture("iron_sword")
            equipped = registry.get("equip_item").callable({"item": "iron_sword", "target": "mainhand"})
            assert equipped.success, equipped.to_payload()
            combat_tool("engage_entity", {"target": "ToolTarget", "timeout_s": 30}, expected_reason="killed")
            for weapon in ("bow", "crossbow"):
                combat_fixture(weapon)
                combat_tool("ranged_attack", {"target": "ToolTarget", "weapon": weapon, "shots": 1, "timeout_s": 15},
                            expected_reason="damage_observed")
            combat_fixture("bow", ammunition=False)
            combat_tool("ranged_attack", {"target": "ToolTarget", "timeout_s": 5}, expected_reason="no_ammunition")
        elif args.case == "menus":
            # Supplied resources and workstations, canonical Agent tool callables;
            # no model planning, gathering or shared-world permission claim.
            command("fill 98 149 -4 108 149 4 minecraft:stone")
            command("fill 98 150 -4 108 154 4 minecraft:air")
            command("setblock 102 150 0 minecraft:crafting_table")
            command("setblock 101 150 2 minecraft:chest")
            command("setblock 101 150 -2 minecraft:furnace")
            command(f"gamemode survival {args.client_name}")
            command(f"tp {args.client_name} 100.5 150 0.5")
            command(f"clear {args.client_name}")
            command(f"recipe give {args.client_name} *")
            wait_at(100.5, 150, 0.5)
            wait_block([102, 150, 0], "crafting_table")
            region = Region("disposable-menu-fixture", (98, 149, -4), (108, 155, 4))
            registry = build_phase1_registry(body, Phase1RuntimeConfig(
                region, body_provider="real_client",
                governance_policy=GovernancePolicy(natural_regions=[region], bot_name=args.client_name),
            ))

            def tool(name: str, params: dict[str, object]) -> dict[str, object]:
                started = time.monotonic()
                result = registry.get(name).callable(params)
                record = {"tool": name, "params": params, "result": result.to_payload(),
                          "elapsed_s": round(time.monotonic() - started, 3),
                          "server_inventory": command(f"data get entity {args.client_name} Inventory")}
                records.append(record)
                print(name, result.success, result.reason, record["elapsed_s"], flush=True)
                assert result.success, result.to_payload()
                return record

            # Two crafts from a non-hotbar source; a second stack remains intact.
            command(f"item replace entity {args.client_name} inventory.0 with minecraft:oak_planks 8")
            command(f"item replace entity {args.client_name} inventory.18 with minecraft:oak_planks 2")
            command(f"item replace entity {args.client_name} inventory.8 with minecraft:cobblestone 3")
            time.sleep(0.3)
            tool("craft_item", {"item": "stick", "count": 8})
            tool("craft_item", {"item": "stone_pickaxe", "count": 1})
            slots = body.perceive("inventory", {}).data["slots"]
            assert slots[27]["count"] == 2 and slots[9]["count"] == 4 and slots[17]["empty"], slots
            assert "minecraft:stone_pickaxe" in records[-1]["server_inventory"]

            # A 3x3 recipe with three native bucket remainders in named slots.
            command(f"clear {args.client_name}")
            for item, amount in (("milk_bucket", 3), ("sugar", 2), ("wheat", 3), ("egg", 1)):
                command(f"give {args.client_name} minecraft:{item} {amount}")
            time.sleep(0.3)
            record = tool("craft_item", {"item": "cake", "count": 1})
            assert "minecraft:cake" in record["server_inventory"] and "minecraft:bucket" in record["server_inventory"]
            assert body.get_state().inventory_counts.get("bucket") == 3

            command(f"clear {args.client_name}")
            command("item replace block 101 150 2 container.0 with minecraft:stone 8")
            time.sleep(0.3)
            tool("transfer_container_item", {"pos": [101, 150, 2], "item": "stone", "count": 3,
                                             "direction": "container_to_bot", "page_size": 12})
            record = tool("transfer_container_item", {"pos": [101, 150, 2], "item": "stone", "count": 2,
                                                      "direction": "bot_to_container", "page_size": 12})
            record["server_container"] = command("data get block 101 150 2 Items")
            assert "count: 7" in record["server_container"] and body.get_state().inventory_counts.get("stone") == 1

            command("item replace block 101 150 2 container.1 with minecraft:stone 63")
            command(f"item replace entity {args.client_name} hotbar.0 with minecraft:stone 2")
            time.sleep(0.3)
            data = action("containerTransfer", {"pos": [101, 150, 2], "direction": "bot_to_container",
                                               "container_slot": 1, "bot_slot": 0, "count": 2, "item": "stone"})
            assert not data["success"] and data["reason"] == "transfer_count_or_capacity_mismatch", data
            assert body.get_state().inventory_counts.get("stone") == 2
            assert "count: 63" in command("data get block 101 150 2 Items")

            # Complete canonical smelt tool: place input/fuel, await cooking,
            # collect output, then reclaim unused fuel through clear_furnace.
            command(f"clear {args.client_name}")
            command(f"item replace entity {args.client_name} inventory.0 with minecraft:raw_iron 1")
            command(f"item replace entity {args.client_name} inventory.8 with minecraft:coal 2")
            time.sleep(0.3)
            record = tool("smelt_item", {"input_item": "raw_iron", "count": 1, "fuel_item": "coal"})
            record["server_furnace"] = command("data get block 101 150 -2 Items")
            assert "minecraft:iron_ingot" in record["server_inventory"]
            assert body.get_state().inventory_counts.get("iron_ingot") == 1
            assert record["server_furnace"].endswith("[]"), record

            # Both furnace directions with an idle fuel stack (no cooking).
            command("item replace block 101 150 -2 container.1 with minecraft:coal 3")
            time.sleep(0.3)
            record = tool("clear_furnace", {"pos": [101, 150, -2]})
            record["server_furnace"] = command("data get block 101 150 -2 Items")
            assert record["server_furnace"].endswith("[]"), record

            # Protocol slots differ from InventoryMenu indices. Verify partial
            # moves, round-trip equipment, and refusal before any inventory change.
            command(f"clear {args.client_name}")
            command(f"item replace entity {args.client_name} inventory.0 with minecraft:oak_planks 8")
            command(f"item replace entity {args.client_name} inventory.18 with minecraft:torch 3")
            command(f"item replace entity {args.client_name} inventory.3 with minecraft:iron_helmet")
            time.sleep(0.3)

            def server_count(slot: int | str, expected: int) -> None:
                path = f"equipment.{slot}.count" if isinstance(slot, str) else f"Inventory[{{Slot:{slot}b}}].count"
                observed = command(f"data get entity {args.client_name} {path}")
                records[-1].setdefault("server_counts", {})[slot] = observed
                assert observed.endswith(f": {expected}"), observed

            for source, destination, count in ((9, 2, 3), (2, 9, 2), (27, 40, 2), (40, 35, 1), (12, 39, 1), (39, 12, 1)):
                data = action("moveItem", {"from_slot": source, "to_slot": destination, "count": count})
                assert data["success"], data
                time.sleep(0.2)
                records[-1]["server_inventory"] = command(f"data get entity {args.client_name} Inventory")
                if destination == 2:
                    server_count(9, 5)
                    server_count(2, 3)
                    server_count(27, 3)
                elif destination == 9:
                    server_count(9, 7)
                    server_count(2, 1)
                elif destination == 40:
                    server_count(27, 1)
                    server_count("offhand", 2)
                elif destination == 35:
                    server_count("offhand", 1)
                    server_count(35, 1)
                else:
                    server_count("head" if destination == 39 else 12, 1)

            bad_moves = [({"from_slot": 45, "to_slot": 0}, "invalid_inventory_slot"),
                         ({"from_slot": 9, "to_slot": 41}, "invalid_inventory_slot"),
                         ({"from_slot": 9, "to_slot": 0, "count": 0}, "move_count_or_capacity_mismatch"),
                         ({"from_slot": 9, "to_slot": 0, "count": 8}, "move_count_or_capacity_mismatch"),
                         ({"from_slot": 9, "to_slot": 27, "count": 1}, "destination_occupied")]
            for params, reason in bad_moves:
                before = command(f"data get entity {args.client_name} Inventory")
                data = action("moveItem", params)
                assert not data["success"] and data["reason"] == reason, data
                assert command(f"data get entity {args.client_name} Inventory") == before

            valid_craft = {"inputs": [{"slot": 9, "item": "oak_planks", "count": 2}],
                           "output": {"slot": 0, "item": "stick", "count": 4}}
            for params, reason in (
                ({**valid_craft, "inputs": [{"slot": 40, "item": "oak_planks", "count": 2}]}, "invalid_recipe_inputs"),
                ({**valid_craft, "output": {"slot": 45, "item": "stick", "count": 4}}, "invalid_recipe_output"),
                ({**valid_craft, "remainders": [{"slot": 41, "item": "bucket", "count": 1}]}, "invalid_remainder_slot"),
            ):
                before = command(f"data get entity {args.client_name} Inventory")
                data = action("craftItem", params)
                assert not data["success"] and data["reason"] == reason, data
                assert command(f"data get entity {args.client_name} Inventory") == before
            for kind, pos, direction in (("containerTransfer", [101, 150, 2], "bot_to_container"),
                                         ("furnaceTransfer", [101, 150, -2], "bot_to_furnace")):
                for bot_slot in (36, 40, 41, 45):
                    before = command(f"data get entity {args.client_name} Inventory")
                    data = action(kind, {"pos": pos, "direction": direction, "bot_slot": bot_slot, "count": 1})
                    assert not data["success"] and data["reason"] == "invalid_transfer_slot", data
                    assert command(f"data get entity {args.client_name} Inventory") == before
        else:
            command("fill 76 149 -4 92 149 4 minecraft:stone")
            command("fill 76 150 -4 92 153 4 minecraft:air")
            command("time set midnight")
            command(f"gamemode survival {args.client_name}")
            for weapon in ("bow", "crossbow"):
                for name in ("RangedFixture", "ToolTarget", "ToolDecoy"):
                    command(f"kill @e[type=zombie,name={name}]")
                command("kill @e[type=arrow,x=80,y=150,z=0,distance=..24]")
                command(f"clear {args.client_name}")
                command(f"give {args.client_name} minecraft:{weapon}")
                command(f"give {args.client_name} minecraft:arrow 16")
                command(f"tp {args.client_name} 80.5 150 0.5")
                command('summon zombie 88.5 150 0.5 {NoAI:1b,Silent:1b,CustomName:"RangedFixture",PersistenceRequired:1b}')
                wait_at(80.5, 150, 0.5)
                data = action("rangedAttack", {"target_name": "RangedFixture", "weapon": weapon, "expected_shots": 1})
                health = command("data get entity @e[type=zombie,name=RangedFixture,limit=1] Health")
                records[-1]["server_target_health"] = health
                assert data["success"] and data["damage_observed"] and data["shots"] == 1, data
                assert data["target_health"] < 20 and "20.0f" not in health, health

            # Existing damage on the target is not evidence of a new shot.
            command("kill @e[type=zombie,name=RangedFixture]")
            command('summon zombie 88.5 150 0.5 {NoAI:1b,Silent:1b,CustomName:"RangedFixture",PersistenceRequired:1b,Health:10.0f}')
            assert command("data get entity @e[type=zombie,name=RangedFixture,limit=1] Health").endswith(": 10.0f")
            command(f"clear {args.client_name}")
            command(f"give {args.client_name} minecraft:bow")
            time.sleep(0.3)
            data = action("rangedAttack", {"target_name": "RangedFixture", "weapon": "bow"})
            assert not data["success"] and data["reason"] == "no_ammunition" and data["shots"] == 0, data
            command(f"give {args.client_name} minecraft:arrow 16")
            time.sleep(0.3)
            data = action("rangedAttack", {"target_name": "RangedFixture", "weapon": "bow"}, cancel=True)
            assert data["status"] == "canceled" and not data["any_process_active"], data
        report["success"] = True
        report["reason"] = "task and server checks passed"
        return 0
    except (Exception, KeyboardInterrupt) as error:
        detail = error.args[0] if error.args else str(error)
        if isinstance(detail, dict):
            detail = detail.get("reason") or {k: detail[k] for k in ("command", "observed") if k in detail} or "assertion failed; see failure trace"
        report["reason"] = f"{type(error).__name__}: {detail}"
        report["failure"] = traceback.format_exc()
        return 1
    finally:
        if args.case == "collection":
            rcon.command("forceload remove 208 -6 236 8")
        report["elapsed_s"] = round(time.monotonic() - started, 3)
        try:
            report["final_state"] = body.ipc.state()
            if report["final_state"].get("any_process_active"):
                body.interrupt("experiment ended")
        except Exception as error:
            report["final_state_error"] = f"{type(error).__name__}: {error}"
        if args.case == "flight":
            try:
                settings = report.get("initial_state", {})
                body.ipc.control("settings", allow_break=settings.get("allow_break", True),
                                 allow_place=settings.get("allow_place", True))
            except Exception as error:
                report["client_settings_cleanup_error"] = str(error)
            try:
                rcon.command("forceload remove 304 0 480 64")
            except Exception as error:
                report["fixture_cleanup_error"] = str(error)
        body.close()
        rcon.close()
        path = args.output or Path("logs") / f"real-client-{args.case}-{time.time_ns()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"{'PASS' if report['success'] else 'FAIL'} {args.case}: {report.get('reason')}", flush=True)
        print(f"Evidence: {path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
