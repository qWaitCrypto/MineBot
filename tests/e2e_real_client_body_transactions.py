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
from minebot.game.rcon import RconClient, RconConfig
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--case", choices=("vertical", "vertical_denied", "handoff", "ranged"), required=True)
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
