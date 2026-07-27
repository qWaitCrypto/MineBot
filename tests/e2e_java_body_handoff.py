#!/usr/bin/env python3
"""Bounded dry-land proof for Java player-to-player item handoff. Not a gate."""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.body import InteractionTransactions
from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import Action, InventorySlot, Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


GIVER = "javahandoff"
RECEIVER = "javareceiver"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("java-handoff", (-16, 0, -16), (16, 320, 16))


def command(rcon: RconClient, value: str, delay: float = 0.05) -> str:
    response = rcon.command(value)
    if delay:
        time.sleep(delay)
    return response


def wait_for_presence(body, *, present: bool, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if body.get_state().missing == (not present):
            return
        time.sleep(0.1)
    raise AssertionError(f"FakePlayer presence did not become {present}")


def inventory_slots(body) -> list[InventorySlot]:
    deadline = time.monotonic() + 3.0
    while True:
        result = read_inventory_slots(body, page_size=46)
        if result.ok and result.complete:
            return [InventorySlot.from_payload(raw) for raw in result.data.get("slots") or []]
        if result.error != "rate_limited" or time.monotonic() >= deadline:
            raise AssertionError(result)
        time.sleep(0.1)


def item_slots(body, item: str) -> list[InventorySlot]:
    return [slot for slot in inventory_slots(body) if slot.item == item]


def item_count(body, item: str) -> int:
    return sum(slot.count for slot in item_slots(body, item))


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    giver = build_body_provider(
        "java", bot_name=GIVER, natural_region=REGION, java_body_url=BODY_URL
    )
    receiver = build_body_provider(
        "java", bot_name=RECEIVER, natural_region=REGION, java_body_url=BODY_URL
    )
    assert giver.java_body is not None and receiver.java_body is not None

    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        for player in (GIVER, RECEIVER):
            command(rcon, f"player {player} kill")
        command(rcon, "kill @e[type=minecraft:item]")
        command(rcon, "fill -4 200 -4 8 203 4 air")
        command(rcon, "fill -4 199 -4 8 199 4 stone")
        command(rcon, f"player {GIVER} spawn")
        wait_for_presence(giver.body, present=True)
        command(rcon, f"tp {GIVER} 0.5 200 0.5 -90 0")
        command(rcon, f"gamemode survival {GIVER}")
        command(rcon, f"clear {GIVER}")

        command(rcon, f"item replace entity {GIVER} inventory.9 with diamond 2")
        missing_before = item_count(giver.body, "minecraft:diamond")
        missing_action = Action.create(
            "handoffItem",
            {
                "receiver": RECEIVER,
                "item": "minecraft:diamond",
                "count": 2,
                "timeout_ticks": 20,
            },
        )
        missing_accepted = giver.body.execute(missing_action)
        missing_terminal = giver.body.await_action_terminal(missing_action.id)
        missing_after = item_count(giver.body, "minecraft:diamond")
        assert missing_accepted.ok and missing_accepted.accepted
        assert missing_terminal.data.get("success") is False, missing_terminal
        assert missing_terminal.data.get("stopped_reason") == "receiver_not_found", missing_terminal
        assert missing_before == missing_after == 2

        command(rcon, f"player {RECEIVER} spawn")
        wait_for_presence(receiver.body, present=True)
        command(rcon, f"tp {RECEIVER} 8.5 200 0.5 90 0")
        command(rcon, f"gamemode survival {RECEIVER}")
        command(rcon, f"clear {RECEIVER}")
        distant_action = Action.create(
            "handoffItem",
            {
                "receiver": RECEIVER,
                "item": "minecraft:diamond",
                "count": 2,
                "timeout_ticks": 20,
            },
        )
        distant_accepted = giver.body.execute(distant_action)
        distant_terminal = giver.body.await_action_terminal(distant_action.id)
        distant_giver_after = item_count(giver.body, "minecraft:diamond")
        distant_receiver_after = item_count(receiver.body, "minecraft:diamond")
        assert distant_accepted.ok and distant_accepted.accepted
        assert distant_terminal.data.get("success") is False, distant_terminal
        assert distant_terminal.data.get("stopped_reason") == "receiver_out_of_range", distant_terminal
        assert distant_giver_after == 2 and distant_receiver_after == 0

        command(rcon, f"clear {GIVER}")
        command(rcon, f"item replace entity {GIVER} inventory.9 with diamond_helmet[damage=7] 1")
        command(rcon, f"tp {RECEIVER} 2.5 200 0.5 90 0")

        giver_pos_before = giver.body.get_state().pos
        receiver_pos_before = receiver.body.get_state().pos
        giver_before = item_count(giver.body, "minecraft:diamond_helmet")
        receiver_before = item_count(receiver.body, "minecraft:diamond_helmet")
        result = InteractionTransactions(giver.body).give_player(
            receiver_name=RECEIVER,
            item="minecraft:diamond_helmet",
            count=1,
            pickup_timeout_s=6.0,
        )
        giver_after = item_count(giver.body, "minecraft:diamond_helmet")
        receiver_after_slots = item_slots(receiver.body, "minecraft:diamond_helmet")
        receiver_after = sum(slot.count for slot in receiver_after_slots)
        giver_pos_after = giver.body.get_state().pos
        receiver_pos_after = receiver.body.get_state().pos

        assert result.success and result.reason == "completed", result
        assert giver_before == 1 and giver_after == 0
        assert receiver_before == 0 and receiver_after == 1
        assert len(receiver_after_slots) == 1
        raw = receiver_after_slots[0].stack_raw or ""
        assert "minecraft:damage" in raw and "7" in raw, raw
        receipt = dict((result.metrics or {}).get("pickup_receipt") or {})
        assert receipt.get("player") == RECEIVER and int(receipt.get("count") or 0) == 1
        assert math.dist(giver_pos_before, giver_pos_after) < 0.25
        assert math.dist(receiver_pos_before, receiver_pos_after) < 0.25

        artifact = {
            "scope": "java_body_handoff",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "body_provider": "java",
            "rcon_role": "fixture_setup_only",
            "missing_receiver": {
                "reason": missing_terminal.data.get("stopped_reason"),
                "giver_before": missing_before,
                "giver_after": missing_after,
            },
            "distant_receiver": {
                "reason": distant_terminal.data.get("stopped_reason"),
                "giver_after": distant_giver_after,
                "receiver_after": distant_receiver_after,
            },
            "delivery": {
                "reason": result.reason,
                "giver_before": giver_before,
                "giver_after": giver_after,
                "receiver_before": receiver_before,
                "receiver_after": receiver_after,
                "pickup_receipt": receipt,
                "damage_metadata_preserved": True,
                "giver_moved": math.dist(giver_pos_before, giver_pos_after),
                "receiver_moved": math.dist(receiver_pos_before, receiver_pos_after),
            },
            "scarpet_body_constructed": False,
        }
        out = Path("logs/agentic-runtime/java-body-handoff-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            command(rcon, "kill @e[type=minecraft:item]")
            for player in (GIVER, RECEIVER):
                command(rcon, f"player {player} kill")
        except Exception:
            pass
        giver.java_body._client.close()
        receiver.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
