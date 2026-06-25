#!/usr/bin/env python3
"""UseTransactions.consume_item e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import UseTransactions
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EConsumeBot"
SKIP_EXIT_CODE = 77


def command(rcon: RconClient, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def entity_int(rcon: RconClient, path: str) -> int:
    raw = command(rcon, f"data get entity {BOT} {path}", delay=0.0)
    return int(raw.rsplit(":", 1)[-1].strip())


def entity_data(rcon: RconClient, path: str) -> str:
    return command(rcon, f"data get entity {BOT} {path}", delay=0.0)


def setup_world(rcon: RconClient) -> None:
    for cmd in [
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "time set day",
        f"player {BOT} kill",
    ]:
        command(rcon, cmd)


def reset_subject(rcon: RconClient) -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        f"clear {BOT}",
        f"tp {BOT} 0 59 0 -90 0",
        f"gamemode survival {BOT}",
        f"effect give {BOT} minecraft:saturation 1 255 true",
        f"effect clear {BOT}",
    ]:
        command(rcon, cmd)


def set_hotbar_slot(rcon: RconClient, slot: int, item_spec: str) -> None:
    command(rcon, f"item replace entity {BOT} hotbar.{slot} with {item_spec}")


def inventory_count(body: ScarpetBody, item: str) -> int:
    wanted = item.removeprefix("minecraft:")
    total = 0
    for slot in body.get_inventory():
        actual = (slot.item or "").removeprefix("minecraft:")
        if actual == wanted:
            total += slot.count
    return total


def active_effects_raw(rcon: RconClient) -> str:
    raw = entity_data(rcon, "active_effects")
    if "Found no elements matching" in raw:
        return ""
    return raw


def give_consumable_variant(rcon: RconClient, variants: list[str]) -> str:
    for cmd_text in variants:
        raw = command(rcon, cmd_text)
        if all(fragment not in raw for fragment in ("Expected whitespace", "Unknown item", "Invalid", "Unknown")):
            return cmd_text
    raise AssertionError(f"unable to give consumable via any variant: {variants}")


def run_bread_happy(rcon: RconClient, body: ScarpetBody, runtime: UseTransactions) -> dict[str, object]:
    reset_subject(rcon)
    command(rcon, f"effect give {BOT} minecraft:hunger 30 255 true", delay=6.0)
    command(rcon, f"effect clear {BOT}")
    set_hotbar_slot(rcon, 0, "bread 2")

    food_before = entity_int(rcon, "foodLevel")
    before_inventory = body.get_inventory()
    result = runtime.consume_item(item="minecraft:bread", use_ticks=80, timeout_s=8.0)
    after_inventory = body.get_inventory()
    food_after = entity_int(rcon, "foodLevel")

    if not result.success or result.reason != "completed":
        raise AssertionError(f"bread consume failed: {result}")
    if result.metrics.get("item_delta", 0) <= 0 or result.metrics.get("food_delta", 0) <= 0:
        raise AssertionError(f"bread consume missing delta: {result.metrics}")

    before_slot = before_inventory[0]
    after_slot = after_inventory[0]
    if before_slot.item != "bread" or before_slot.count != 2:
        raise AssertionError(f"unexpected bread setup before consume: {before_slot}")
    if after_slot.count >= before_slot.count:
        raise AssertionError(f"bread count did not decrease: before={before_slot} after={after_slot}")
    if food_after <= food_before:
        raise AssertionError(f"food did not increase: before={food_before} after={food_after}")

    return {
        "reason": result.reason,
        "before_count": before_slot.count,
        "after_count": after_slot.count,
        "food_before": food_before,
        "food_after": food_after,
        "metrics": result.metrics,
    }


def run_bread_already_full(rcon: RconClient, runtime: UseTransactions) -> dict[str, object]:
    reset_subject(rcon)
    set_hotbar_slot(rcon, 0, "bread 1")

    food_before = entity_int(rcon, "foodLevel")
    result = runtime.consume_item(item="minecraft:bread", use_ticks=80, timeout_s=8.0)
    food_after = entity_int(rcon, "foodLevel")

    if not result.success or result.reason != "already_full":
        raise AssertionError(f"bread already-full truth failed: {result}")
    if result.metrics.get("item_delta") != 0 or result.metrics.get("food_delta") != 0:
        raise AssertionError(f"already-full bread mutated state: {result.metrics}")
    if food_after != food_before:
        raise AssertionError(f"already-full bread changed food unexpectedly: before={food_before} after={food_after}")

    return {
        "reason": result.reason,
        "food_before": food_before,
        "food_after": food_after,
        "metrics": result.metrics,
    }


def run_potion_effect_delta(rcon: RconClient, body: ScarpetBody, runtime: UseTransactions) -> dict[str, object]:
    reset_subject(rcon)
    give_command = give_consumable_variant(
        rcon,
        [
            f"give {BOT} minecraft:potion[minecraft:potion_contents=swiftness] 1",
            f"give {BOT} potion[potion_contents=swiftness] 1",
            f"give {BOT} potion{{Potion:\"minecraft:swiftness\"}} 1",
        ],
    )

    before_effects = active_effects_raw(rcon)
    result = runtime.consume_item(item="minecraft:potion", use_ticks=80, timeout_s=8.0)
    after_state = body.get_state()
    after_effects = active_effects_raw(rcon)

    if not result.success or result.reason != "completed":
        raise AssertionError(f"potion consume failed: {result}")
    if result.metrics.get("effect_delta", 0) <= 0:
        raise AssertionError(f"potion consume did not report effect delta: {result.metrics}")
    added = result.metrics.get("effects_added") or []
    if not any(str(effect.get("id")) == "speed" for effect in added):
        raise AssertionError(f"potion consume missing speed effect add: {result.metrics}")
    if inventory_count(body, "glass_bottle") <= 0:
        raise AssertionError(f"potion consume did not leave bottle remainder: {body.get_inventory()}")
    if "minecraft:speed" not in after_effects:
        raise AssertionError(f"potion consume did not apply speed on live server: {after_effects}")
    if not any(str((effect or {}).get("id")) == "speed" for effect in (after_state.effects or [])):
        raise AssertionError(f"state effects missing speed after potion: {after_state.effects}")

    return {
        "reason": result.reason,
        "give_command": give_command,
        "before_effects": before_effects,
        "after_effects": after_effects,
        "metrics": result.metrics,
    }


def run_milk_bucket_non_food(rcon: RconClient, body: ScarpetBody, runtime: UseTransactions) -> dict[str, object]:
    reset_subject(rcon)
    command(rcon, f"effect give {BOT} minecraft:speed 30 0 true", delay=0.2)
    set_hotbar_slot(rcon, 0, "milk_bucket 1")

    before_effects = active_effects_raw(rcon)
    result = runtime.consume_item(item="minecraft:milk_bucket", use_ticks=80, timeout_s=8.0)
    after_state = body.get_state()
    after_effects = active_effects_raw(rcon)

    if not result.success or result.reason != "completed":
        raise AssertionError(f"milk bucket consume failed: {result}")
    if result.metrics.get("effect_delta", 0) <= 0:
        raise AssertionError(f"milk bucket consume did not report effect delta: {result.metrics}")
    removed = result.metrics.get("effects_removed") or []
    if not any(str(effect.get("id")) == "speed" for effect in removed):
        raise AssertionError(f"milk bucket consume missing speed effect removal: {result.metrics}")
    if inventory_count(body, "bucket") <= 0:
        raise AssertionError(f"milk bucket consume did not leave bucket remainder: {body.get_inventory()}")
    if "minecraft:speed" in after_effects:
        raise AssertionError(f"milk bucket did not clear speed effect on live server: {after_effects}")
    if after_state.effects:
        raise AssertionError(f"state effects not cleared after milk bucket: {after_state.effects}")

    return {
        "reason": result.reason,
        "before_effects": before_effects,
        "after_effects": after_effects,
        "metrics": result.metrics,
    }


def main() -> None:
    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        runtime = UseTransactions(body)
        spawn_or_fail(body, (0, 59, 0))

        print(
            {
                "bread_happy": run_bread_happy(rcon, body, runtime),
                "bread_already_full": run_bread_already_full(rcon, runtime),
                "potion_effect_delta": run_potion_effect_delta(rcon, body, runtime),
                "milk_bucket_non_food": run_milk_bucket_non_food(rcon, body, runtime),
            }
        )


if __name__ == "__main__":
    main()
