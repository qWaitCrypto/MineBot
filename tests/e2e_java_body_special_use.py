#!/usr/bin/env python3
"""Bounded dry-land proof for governed Java ignite and sow actions. Not a gate."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_provider import build_body_provider
from minebot.body import InteractionTransactions, UseTransactions
from minebot.body.inventory_read import read_inventory_slots
from minebot.contract import InventorySlot, Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "javaspecialuse"
BODY_URL = "ws://127.0.0.1:8767"
NATURAL = Region("java-special-use", (-16, 0, -16), (16, 320, 16))
PROTECTED_FIRE = Region("protected-fire", (4, 200, 0), (4, 200, 0))
FIRE = (2, 200, 0)
DENIED_FIRE = (4, 200, 0)
FARMLAND = (2, 199, 2)
CROP = (2, 200, 2)


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


def block_type(body, pos: tuple[int, int, int]) -> str:
    result = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    assert result.ok and result.complete, result
    return str(result.data.get("type"))


def item_count(body, item: str) -> int:
    deadline = time.monotonic() + 3.0
    while True:
        result = read_inventory_slots(body, page_size=46)
        if result.ok and result.complete:
            return sum(
                slot.count
                for slot in (
                    InventorySlot.from_payload(raw) for raw in result.data.get("slots") or []
                )
                if slot.item == item
            )
        if result.error != "rate_limited" or time.monotonic() >= deadline:
            raise AssertionError(result)
        time.sleep(0.1)


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    provider = build_body_provider(
        "java", bot_name=BOT, natural_region=NATURAL, java_body_url=BODY_URL
    )
    assert provider.java_body is not None
    provider.governance.protected_regions.append(PROTECTED_FIRE)
    body = provider.body

    try:
        command(rcon, "carpet commandPlayer true")
        command(rcon, "carpet allowSpawningOfflinePlayers true")
        command(rcon, "gamerule doFireTick false")
        command(rcon, f"player {BOT} kill")
        command(rcon, "fill -2 199 -2 10 204 6 air")
        command(rcon, "fill -2 198 -2 10 198 6 stone")
        command(rcon, f"setblock {FIRE[0]} {FIRE[1] - 1} {FIRE[2]} netherrack")
        command(rcon, f"setblock {DENIED_FIRE[0]} {DENIED_FIRE[1] - 1} {DENIED_FIRE[2]} netherrack")
        command(rcon, f"setblock {FARMLAND[0]} {FARMLAND[1]} {FARMLAND[2]} farmland[moisture=7]")
        command(rcon, f"player {BOT} spawn")
        wait_for_presence(body, present=True)
        command(rcon, f"tp {BOT} 0.5 200 0.5 0 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with flint_and_steel 1")

        fire = UseTransactions(body).use_on_block(
            pos=FIRE,
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
            look_target=(FIRE[0] + 0.5, FIRE[1] - 0.2, FIRE[2] + 0.5),
            timeout_s=6.0,
        )
        assert fire.success and fire.reason == "completed", fire
        assert block_type(body, FIRE) == "minecraft:fire"
        fire_method = ((fire.metrics or {}).get("use") or {}).get("metrics", {}).get("method")
        assert fire_method in {"physical", "substitute"}, fire.metrics

        denied_before = item_count(body, "minecraft:flint_and_steel")
        denied = UseTransactions(body).use_on_block(
            pos=DENIED_FIRE,
            item="minecraft:flint_and_steel",
            expected_block_types=("fire",),
            look_target=(DENIED_FIRE[0] + 0.5, DENIED_FIRE[1] - 0.2, DENIED_FIRE[2] + 0.5),
            timeout_s=4.0,
        )
        denied_after = item_count(body, "minecraft:flint_and_steel")
        assert not denied.success and "governance_denied:protected_region" in denied.reason, denied
        assert block_type(body, DENIED_FIRE) == "minecraft:air"
        assert denied_before == denied_after == 1

        command(rcon, f"clear {BOT}")
        command(rcon, f"item replace entity {BOT} hotbar.0 with wheat_seeds 3")
        time.sleep(1.1)
        seeds_before = item_count(body, "minecraft:wheat_seeds")
        sow = InteractionTransactions(body, governance=provider.governance).sow_crop(
            seed_item="minecraft:wheat_seeds",
            farmland_pos=FARMLAND,
            use_timeout_s=6.0,
        )
        seeds_after = item_count(body, "minecraft:wheat_seeds")
        assert sow.success and sow.reason == "sown", sow
        assert block_type(body, CROP) == "minecraft:wheat"
        assert seeds_before == 3 and seeds_after == 2

        artifact = {
            "scope": "java_body_special_use",
            "formal_gate": False,
            "bounded": True,
            "environment": "dry_land",
            "body_provider": "java",
            "rcon_role": "fixture_setup_only",
            "ignite": {
                "reason": fire.reason,
                "block_after": block_type(body, FIRE),
                "method": fire_method,
            },
            "protected_ignite": {
                "reason": denied.reason,
                "block_after": block_type(body, DENIED_FIRE),
                "item_before": denied_before,
                "item_after": denied_after,
            },
            "sow": {
                "reason": sow.reason,
                "crop_after": block_type(body, CROP),
                "seeds_before": seeds_before,
                "seeds_after": seeds_after,
            },
            "scarpet_body_constructed": False,
        }
        out = Path("logs/agentic-runtime/java-body-special-use-20260727.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(json.dumps(artifact, indent=2))
        return 0
    finally:
        try:
            command(rcon, f"player {BOT} kill")
        except Exception:
            pass
        provider.java_body._client.close()
        rcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
