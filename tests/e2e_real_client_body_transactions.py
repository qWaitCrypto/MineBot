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

from minebot.contract import Action
from minebot.contract import Region
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.game.governance import GovernancePolicy
from minebot.game.rcon import RconClient, RconConfig
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--case", choices=("vertical", "vertical_denied", "handoff", "ranged", "menus"), required=True)
    parser.add_argument("--client-name", default="MineBotCombat")
    parser.add_argument("--receiver-name", default="Receiver")
    args = parser.parse_args()
    rcon = RconClient(RconConfig(password=os.environ.get("MINEBOT_TEST_RCON_PASSWORD", "test")))
    body = RealClientBody(args.client_name, RealClientIpc(timeout_s=30))
    records: list[dict[str, object]] = []

    def command(text: str) -> str:
        return rcon.command(text)

    def wait_at(x: float, y: float, z: float) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pos = body.get_state().pos
            if pos and sum((a - b) ** 2 for a, b in zip(pos, (x, y, z))) < 0.02:
                time.sleep(0.3)
                return
            time.sleep(0.1)
        raise AssertionError("client did not observe the fixture teleport")

    def action(name: str, params: dict[str, object], *, cancel: bool = False) -> dict[str, object]:
        request = Action.create(name, {"timeout_s": 30, **params})
        accepted = body.execute(request)
        assert accepted.ok and accepted.accepted, accepted
        if cancel:
            time.sleep(0.4)
            assert body.interrupt("fixture cancellation").ok
        event = body.await_action_terminal(request.id, timeout_s=40)
        data = dict(event.data)
        records.append({"action": name, "params": params, "terminal": data})
        print(name, data["status"], data["reason"], data.get("elapsed_ms"), flush=True)
        return data

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
        if args.case == "vertical_denied":
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
        elif args.case == "handoff":
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
            data = action("handoffItem", {"receiver": args.receiver_name, "item": "diamond", "slot": 0,
                                         "count": 2, "pickup_timeout_ticks": 100})
            assert data["success"] and data["handoff_receipt"] and data["handoff_picked_up"] == 2, data
            recipient_inventory = command(f"data get entity {args.receiver_name} Inventory")
            records[-1]["server_recipient_inventory"] = recipient_inventory
            assert 'count: 2' in recipient_inventory and 'minecraft:diamond' in recipient_inventory, recipient_inventory
            assert command("execute if entity @e[type=item,x=0,y=70,z=0,distance=..8]") == "Test failed"

            # A real player with a full inventory cannot receive the drop.
            command(f"clear {args.receiver_name}")
            command(f"give {args.receiver_name} minecraft:cobblestone 2304")
            command(f"give {args.client_name} minecraft:diamond 2")
            time.sleep(0.3)
            data = action("handoffItem", {"receiver": args.receiver_name, "item": "diamond", "slot": 0,
                                         "count": 2, "pickup_timeout_ticks": 20})
            assert not data["success"] and not data["handoff_receipt"] and data["handoff_picked_up"] == 0, data
            assert data["reason"] == "receiver_pickup_unconfirmed", data
            assert "minecraft:diamond" not in command(f"data get entity {args.receiver_name} Inventory")
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
                command("kill @e[type=zombie,name=RangedFixture]")
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
            command(f"clear {args.client_name}")
            command(f"give {args.client_name} minecraft:bow")
            time.sleep(0.3)
            data = action("rangedAttack", {"target_name": "RangedFixture", "weapon": "bow"})
            assert not data["success"] and data["reason"] == "no_ammunition" and data["shots"] == 0, data
            command(f"give {args.client_name} minecraft:arrow 16")
            time.sleep(0.3)
            data = action("rangedAttack", {"target_name": "RangedFixture", "weapon": "bow"}, cancel=True)
            assert data["status"] == "canceled" and not data["any_process_active"], data
    finally:
        body.close()
        rcon.close()
        path = Path("logs") / f"real-client-{args.case}-{time.time_ns()}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"Evidence: {path}", flush=True)


if __name__ == "__main__":
    main()
