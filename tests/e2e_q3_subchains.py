#!/usr/bin/env python3
"""Short Q3 production-ingress replay for the FakePlayer goal.

This is a deterministic subchain gate, not the AG-FP30 long run.  It builds
the same Phase-1 registry used by the real agent, places a bounded difficult
fixture (water band, raised bank, vertical resources), and drives only public
tool contracts.  The evaluator reads inventory, equipment, entity and block
facts after every step; no model text or route hint is accepted as success.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.phase1_runtime import (  # noqa: E402
    Phase1RuntimeConfig,
    build_phase1_agent_runtime,
    inventory_count,
)
from minebot.brain.lifecycle import LifecycleState  # noqa: E402
from minebot.brain.registry import execute_tool  # noqa: E402
from minebot.contract import Region  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "Q3SubchainProbe"
START = (8, 72, -4)
REGION = Region("q3-subchains", (-16, 0, -16), (32, 120, 16))
WATER = (1, 6, -64, 64)
FLOWERS = {
    "dandelion": (9, 71, -4),
    "poppy": (12, 71, -4),
    "blue_orchid": (15, 71, -4),
}
FLOWER_BACKUPS = {
    "dandelion": (10, 71, -4),
    "poppy": (13, 71, -4),
    "blue_orchid": (16, 71, -4),
}
LOGS = [(14, 72, -1), (14, 73, -1), (14, 74, -1), (15, 72, -1), (15, 73, -1), (15, 74, -1)]
# A separate, low, supported pair supplies the later shield-plank request.
# The primary tree remains in the fixture for the tree-domain path; this pair
# keeps the material-chain substep from silently becoming a tree-top egress
# gate after the first wood request has consumed the easy logs.
SHIELD_LOGS = [(18, 70, 4), (18, 71, 4), (18, 72, 4)]
COAL = [(x, 70, -3) for x in range(9, 17)] + [(x, 70, 4) for x in range(9, 17)]
# Keep several small ore clusters rather than one contiguous vein.  A single
# target can legitimately be occluded after navigation; the resource domain
# should then choose another spatial cluster instead of having the candidate
# blacklist erase the entire seven-ingot supply.
IRON = [
    (x, y, z)
    for x in (9, 12, 15)
    for y in (70, 71)
    for z in (3, 4)
]
# Twenty-four exposed stone cells on the bank's lower working layer.  The
# production chain consumes some stone for the pickaxe before the explicit
# furnace supply request, so the extra local cells provide honest candidate
# retries when a vanilla drop drifts away before pickup or one candidate
# cluster is blacklisted.  The irregular rows keep the supply away from the
# separate oak-log fixture.
STONE = [(x, 71, -1) for x in range(6, 18)] + [(x, 71, 0) for x in range(6, 18)]
PICKUP_SPOT = (14, 71, -4)
ANIMAL_POSITIONS = {"pig": (10, 71, -3), "cow": (12, 71, -3), "sheep": (14, 71, -4)}


def command(rcon: RconClient, text: str, delay: float = 0.04) -> str:
    result = rcon.command(text)
    if delay:
        time.sleep(delay)
    return result


def setup_fixture(rcon: RconClient) -> None:
    for text in (
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        "forceload add -16 -64 32 64",
        "fill -16 70 -64 32 78 0 air",
        "fill -16 70 1 32 78 64 air",
        # Keep the lower substrate distinct from the exposed stone vein.  A
        # broad stone floor would flood the generic stone domain with buried
        # candidates and turn the Q3 gate into a fixture-selection test.
        "fill -16 69 -64 32 69 0 deepslate",
        "fill -16 69 1 32 69 64 deepslate",
        "fill 8 70 -4 16 71 4 dirt",
        # Extend the bank for two extra stone cells without putting stone
        # directly below the separate oak-log fixture.
        "setblock 6 70 -1 dirt",
        "setblock 7 70 -1 dirt",
        "setblock 6 70 0 dirt",
        "setblock 7 70 0 dirt",
        "setblock 17 70 -1 dirt",
        "setblock 17 70 0 dirt",
        # Flowers share a low, open bank row.  This keeps their drops and
        # adjacent candidates in the same dry walkable layer instead of
        # embedding an item under the neighboring plant voxel.
        "fill 8 71 -4 16 71 -4 air",
        # Keep a small open pickup apron around that row so vanilla item
        # drift cannot settle the drop inside a solid feet cell.
        "fill 8 71 -5 16 71 -3 air",
        # Leave one dry, lower-layer slot open for the explicit pickup probe.
        f"setblock {PICKUP_SPOT[0]} {PICKUP_SPOT[1]} {PICKUP_SPOT[2]} air",
        # Entities need an actual feet cell, not a dirt voxel, so the shared
        # stand-domain can find a legal adjacent position for every species.
        *(f"setblock {x} {y} {z} air" for x, y, z in ANIMAL_POSITIONS.values()),
        "script in minebot run minebot_reset()",
    ):
        command(rcon, text)

    # Keep the water wall inside the bounded fixture and place the resources
    # on a raised bank.  The scene is intentionally generic: production code
    # sees only block/entity facts, never these coordinates.
    command(rcon, f"fill {WATER[0]} 70 {WATER[2]} {WATER[1]} 70 {WATER[3]} water", delay=0.0)
    for item, (x, y, z) in FLOWERS.items():
        command(rcon, f"setblock {x} {y} {z} {item}", delay=0.0)
        # Keep one same-type natural candidate nearby.  A vanilla flower drop
        # can drift outside the first pickup approach before its delay closes;
        # the second block lets the normal resource transaction reselect a
        # candidate rather than making this gate depend on one item trajectory.
        backup_x, backup_y, backup_z = FLOWER_BACKUPS[item]
        command(rcon, f"setblock {backup_x} {backup_y} {backup_z} {item}", delay=0.0)
    for x, y, z in LOGS:
        command(rcon, f"setblock {x} {y} {z} oak_log", delay=0.0)
    # Keep the bottom log on an explicit natural support and leave dirt beside
    # the column so the upper log remains reachable after the first break.
    command(rcon, "setblock 18 69 4 dirt", delay=0.0)
    command(rcon, "setblock 17 70 4 dirt", delay=0.0)
    command(rcon, "setblock 17 71 4 dirt", delay=0.0)
    command(rcon, "setblock 19 70 4 dirt", delay=0.0)
    for x, y, z in SHIELD_LOGS:
        command(rcon, f"setblock {x} {y} {z} oak_log", delay=0.0)
    for x, y, z in COAL:
        command(rcon, f"setblock {x} {y} {z} coal_ore", delay=0.0)
        command(rcon, f"setblock {x} {y + 1} {z} air", delay=0.0)
    for x, y, z in IRON:
        command(rcon, f"setblock {x} {y} {z} iron_ore", delay=0.0)
    # The stone vein is embedded in the bank, so the stone subchain exercises
    # a vertical target domain instead of a flat happy-path block.
    for x, y, z in STONE:
        command(rcon, f"setblock {x} {y} {z} stone", delay=0.0)
    # Small natural palette breaks prevent the deliberately bounded test bank
    # from being classified as an axis-symmetric player platform.  They supply
    # evidence to the existing structure-risk assessor; they do not bypass or
    # alter governance.
    for x in (9, 12, 15):
        command(rcon, f"setblock {x} 70 -2 gravel", delay=0.0)


def reset_bot(rcon: RconClient, body: ScarpetBody) -> None:
    command(rcon, "kill @e[type=item]", delay=0.0)
    command(rcon, f"player {BOT} kill")
    spawn_or_fail(body, START)
    for text in (
        f"tp {BOT} {START[0]} {START[1]} {START[2]} -90 0",
        f"gamemode survival {BOT}",
        f"clear {BOT}",
        f"effect clear {BOT}",
        "script in minebot run minebot_reset()",
    ):
        command(rcon, text)


def make_parts(body: ScarpetBody, goal: str):
    return build_phase1_agent_runtime(
        body=body,
        goal_text=goal,
        model_provider=None,
        config=Phase1RuntimeConfig(natural_region=REGION),
        agent_name="Q3SubchainAgent",
        language="English",
    )


def invoke(parts, name: str, params: dict[str, object]) -> dict[str, object]:
    payload = execute_tool(parts.registry.get(name), params, parts.runtime.weld_context)
    if not isinstance(payload, dict):
        raise AssertionError(f"{name} returned a non-object payload: {payload!r}")
    return payload


def assert_success(result: dict[str, object], label: str) -> dict[str, object]:
    if result.get("success") is not True:
        raise AssertionError(f"{label} failed: {json.dumps(result, sort_keys=True)}")
    return result


def slot_item(body: ScarpetBody, slot_number: int) -> str | None:
    for slot in body.get_inventory():
        if slot.slot == slot_number:
            return None if slot.item is None else slot.item.removeprefix("minecraft:")
    return None


def selected_mainhand_item(rcon: RconClient, body: ScarpetBody) -> tuple[int | None, str | None]:
    """Read the server-selected hotbar slot before evaluating mainhand facts."""

    raw = rcon.command(f"data get entity {BOT} SelectedItemSlot")
    match = re.search(r"(-?\d+)\s*$", str(raw))
    if match is None:
        return None, None
    selected_slot = int(match.group(1))
    return selected_slot, slot_item(body, selected_slot)


def run_flowers(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    parts = make_parts(body, "collect three flower types")
    results: dict[str, object] = {}
    for flower in FLOWERS:
        result = assert_success(
            invoke(
                parts,
                "collect_resource",
                {
                    "item": flower,
                    "count": 1,
                    "constraints": {"radius": 24, "max_candidates": 8, "max_mutating_calls": 8},
                },
            ),
            f"flower:{flower}",
        )
        if inventory_count(body, flower) < 1:
            raise AssertionError(f"flower:{flower} did not change authoritative inventory: {result}")
        results[flower] = {
            "reason": result.get("reason"),
            "inventory_count": inventory_count(body, flower),
            "navigation_fallback_attempts": (result.get("metrics") or {}).get("navigation_fallback_attempts"),
        }
    return results


def summon_animal(rcon: RconClient, animal: str, pos: tuple[int, int, int]) -> None:
    x, y, z = pos
    command(
        rcon,
        f'summon {animal} {x + 0.5} {y} {z + 0.5} '
        '{NoAI:1b,PersistenceRequired:1b,Tags:["minebot.q3_target"]}',
    )


def combat_drop_observed(
    body: ScarpetBody,
    accepted: tuple[str, ...],
    before: dict[str, int],
    after: dict[str, int],
) -> str | None:
    if any(after[item] > before[item] for item in accepted):
        return "inventory_delta"
    perception = body.perceive("nearbyEntities", {"radius": 10, "limit": 64, "types": ["item"]})
    if not perception.ok:
        return None
    aliases = tuple(item.removeprefix("minecraft:").replace("_", " ").lower() for item in accepted)
    for entity in perception.data.get("entities") or []:
        name = str(entity.get("name") or "").lower()
        if any(alias in name for alias in aliases):
            return "nearby_item"
    return None


def run_animals(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    parts = make_parts(body, "find and engage pig cow sheep and keep their drops")
    # Animals stand on the bank's lower walkable layer.  The flower targets
    # occupy y=71, so the Body remains at the same feet level after collecting
    # them; spawning animals on the bank top (y=72) puts the feet distance just
    # outside the governed 2.5-block melee range.
    animal_positions = ANIMAL_POSITIONS
    accepted = {
        "pig": ("porkchop",),
        "cow": ("beef", "leather"),
        "sheep": ("mutton", "white_wool", "wool"),
    }
    output: dict[str, object] = {}
    for animal, pos in animal_positions.items():
        summon_animal(rcon, animal, pos)
        found = assert_success(
            invoke(parts, "search_for_entity", {"entity_types": [animal], "search_radius": 24, "max_distance": 4.5, "timeout_s": 25}),
            f"search:{animal}",
        )
        before_combat = {item: inventory_count(body, item) for item in accepted[animal]}
        engaged = assert_success(
            invoke(
                parts,
                "engage_entity",
                {"target": animal, "attack_range": 2.5, "cooldown_ticks": 8, "timeout_s": 25, "disengage_health": 0},
            ),
            f"engage:{animal}",
        )
        # Scarpet emits engageDone on the kill tick; vanilla loot spawning and
        # fake-player pickup can settle on the following server tick.
        time.sleep(0.75)
        after_combat = {item: inventory_count(body, item) for item in accepted[animal]}
        combat_drop_evidence = combat_drop_observed(body, accepted[animal], before_combat, after_combat)
        if combat_drop_evidence is None:
            raise AssertionError(
                f"{animal} had no authoritative combat-drop evidence: "
                f"before={before_combat} after={after_combat} engaged={engaged}"
            )

        # The fake player can auto-pick a kill drop while the combat
        # transaction is still closing.  Keep that authoritative delta as
        # combat evidence, then place a delayed second drop away from the bot
        # so the shared pickup transaction is exercised independently.
        pickup_item = accepted[animal][0]
        drop_x, drop_y, drop_z = PICKUP_SPOT
        command(
            rcon,
            f'summon item {drop_x + 0.5:.3f} {drop_y + 0.2:.3f} {drop_z + 0.5:.3f} '
            f'{{NoGravity:1b,Item:{{id:"minecraft:{pickup_item}",count:1}},PickupDelay:20s,Tags:["minebot.q3_pickup"]}}',
            delay=0.0,
        )
        before_pickup_counts = {item: inventory_count(body, item) for item in accepted[animal]}
        picked = assert_success(
            invoke(
                parts,
                "pickup_items",
                {"expected_items": list(accepted[animal]), "minimum_count": 1, "radius": 10, "max_scan_rounds": 3, "candidate_budget": 8, "max_wall_s": 25},
            ),
            f"pickup:{animal}",
        )
        after_pickup_counts = {item: inventory_count(body, item) for item in accepted[animal]}
        before_pickup = sum(before_pickup_counts.values())
        after_pickup = sum(after_pickup_counts.values())
        if after_pickup <= before_pickup:
            raise AssertionError(
                f"{animal} explicit pickup had no authoritative inventory delta: "
                f"before={before_pickup_counts} after={after_pickup_counts} picked={picked}"
            )
        output[animal] = {
            "search_reason": found.get("reason"),
            "engage_reason": engaged.get("reason"),
            "engage_attacks": (engaged.get("metrics") or {}).get("attacks"),
            "combat_drop_evidence": combat_drop_evidence,
            "pickup_reason": picked.get("reason"),
            "combat_drop_before": before_combat,
            "combat_drop_after": after_combat,
            "pickup_item": pickup_item,
            "pickup_before": before_pickup_counts,
            "pickup_after": after_pickup_counts,
        }
    return output


def run_production_chain(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    # Animal terminal facts are already captured in inventory.  Remove
    # duplicate/late loot entities before the next subchain so its pickup
    # candidate budget measures the wood/ore chain rather than prior drops.
    command(rcon, "kill @e[type=item]", delay=0.0)
    parts = make_parts(body, "build a stone and iron tool chain with torches")
    logs = assert_success(
        invoke(parts, "collect_resource", {"item": "logs", "count": 4, "constraints": {"radius": 24, "max_candidates": 12, "max_mutating_calls": 16}}),
        "wood:logs",
    )
    stone_tool = assert_success(invoke(parts, "ensure_tool_for", {"resource": "stone_pickaxe"}), "wood-to-stone")
    stone_count = inventory_count(body, "cobblestone")
    if inventory_count(body, "stone_pickaxe") < 1:
        raise AssertionError(f"wood-to-stone terminal facts missing: stone={stone_count} result={stone_tool}")

    # The stone pickaxe consumes three cobblestone.  Collect the eight more
    # needed by the furnace through the same public resource transaction;
    # having enough blocks in the fixture is not itself an inventory fact.
    furnace_stone = assert_success(
        # Keep this subchain bound to the deliberately local stone fixture.
        # The surrounding test world still contains its ordinary underground
        # stone below the bounded scene; including that unrelated domain would
        # turn the Q3 gate into a deep-world exploration test after the local
        # supply is exhausted.
        invoke(parts, "collect_resource", {"item": "cobblestone", "count": 8, "constraints": {"radius": 8, "max_candidates": 16, "max_mutating_calls": 24}}),
        "iron:furnace_stone",
    )
    if inventory_count(body, "cobblestone") < 8:
        raise AssertionError(f"furnace cobblestone terminal fact missing: {furnace_stone}")

    coal = assert_success(
        # One coal crafts four torches.  Keep four coal in reserve as furnace
        # fuel after producing the authoritative sixteen-torch terminal fact.
        invoke(parts, "collect_resource", {"item": "coal", "count": 8, "constraints": {"radius": 24, "max_candidates": 12, "max_mutating_calls": 16}}),
        "coal:collect",
    )
    sticks = assert_success(invoke(parts, "craft_item", {"item": "stick", "count": 4}), "coal:sticks")
    torches = assert_success(invoke(parts, "craft_item", {"item": "torch", "count": 16}), "coal:torches")
    if inventory_count(body, "torch") < 16:
        raise AssertionError(f"torch terminal fact missing: {torches}")

    raw_iron = assert_success(
        invoke(parts, "collect_resource", {"item": "raw_iron", "count": 7, "constraints": {"radius": 24, "max_candidates": 16, "max_mutating_calls": 24}}),
        "iron:raw",
    )
    workstation_planks = None
    workstation = None
    # The stone-tool acquisition may have reclaimed its temporary table.  Use
    # authoritative inventory truth and the normal public craft ingress to
    # restore a workstation when needed; never inject one through setup.
    if inventory_count(body, "crafting_table") < 1:
        workstation_planks = assert_success(
            invoke(parts, "craft_item", {"item": "oak_planks", "count": 4}),
            "iron:workstation_planks",
        )
        workstation = assert_success(
            invoke(parts, "craft_item", {"item": "crafting_table", "count": 1}),
            "iron:workstation",
        )
    # Use the normal composition-visible craft/smelt/equip tools.  The carried
    # furnace path is part of the existing transaction and is reclaimed by it.
    furnace = assert_success(
        invoke(parts, "craft_item", {"item": "furnace", "count": 1, "keep_temporary_table": True}),
        "iron:furnace",
    )
    smelt = assert_success(invoke(parts, "smelt_item", {"input_item": "raw_iron", "count": 7}), "iron:smelt")
    # The earlier stone/tool/torch steps legitimately consume most of the
    # initial wood.  Acquire the remaining shield planks through the same
    # public resource ingress instead of relying on a fixture-side inventory
    # injection or assuming the first wood request covers the whole chain.
    shield_logs = assert_success(
        invoke(
            parts,
            "collect_resource",
            # The shield-log fixture is ~8 blocks from the post-ore body
            # position. Keep this bounded to the Q3 scene while including the
            # complete local tree domain after the first log request.
            {"item": "logs", "count": 3, "constraints": {"radius": 9, "max_candidates": 8, "max_mutating_calls": 16}},
        ),
        "iron:shield_logs",
    )
    shield_planks = assert_success(invoke(parts, "craft_item", {"item": "oak_planks", "count": 8}), "iron:shield_planks")
    shield_sticks = assert_success(invoke(parts, "craft_item", {"item": "stick", "count": 4}), "iron:shield_sticks")
    shield = assert_success(
        invoke(
            parts,
            "craft_item",
            {"item": "shield", "count": 1, "search_radius": 16, "keep_temporary_table": True},
        ),
        "iron:shield",
    )
    pickaxe = assert_success(
        invoke(
            parts,
            "craft_item",
            {"item": "iron_pickaxe", "count": 1, "search_radius": 16, "keep_temporary_table": True},
        ),
        "iron:pickaxe",
    )
    equipped_shield = assert_success(invoke(parts, "equip_item", {"item": "shield", "target": "offhand"}), "iron:equip_shield")
    equipped_pickaxe = assert_success(invoke(parts, "equip_item", {"item": "iron_pickaxe", "target": "mainhand"}), "iron:equip_pickaxe")
    selected_slot, selected_item = selected_mainhand_item(rcon, body)
    if slot_item(body, 40) != "shield" or selected_item != "iron_pickaxe":
        raise AssertionError(
            f"equipment terminal facts missing: offhand={slot_item(body, 40)} "
            f"mainhand={selected_item} selected_slot={selected_slot} "
            f"shield={equipped_shield} pickaxe={equipped_pickaxe}"
        )
    if inventory_count(body, "iron_ingot") < 3:
        raise AssertionError(f"iron reserve was consumed incorrectly: {inventory_count(body, 'iron_ingot')}")
    return {
        "logs": {"reason": logs.get("reason"), "count": inventory_count(body, "oak_log")},
        "wood_to_stone": {"reason": stone_tool.get("reason"), "cobblestone": stone_count},
        "furnace_stone": {"reason": furnace_stone.get("reason"), "cobblestone": inventory_count(body, "cobblestone")},
        "coal": {"reason": coal.get("reason"), "torch_count": inventory_count(body, "torch")},
        "iron": {
            "raw_reason": raw_iron.get("reason"),
            "workstation_planks_reason": None if workstation_planks is None else workstation_planks.get("reason"),
            "workstation_reason": None if workstation is None else workstation.get("reason"),
            "furnace_reason": furnace.get("reason"),
            "smelt_reason": smelt.get("reason"),
            "shield_logs_reason": shield_logs.get("reason"),
            "shield_planks_reason": shield_planks.get("reason"),
            "shield_sticks_reason": shield_sticks.get("reason"),
            "shield_reason": shield.get("reason"),
            "pickaxe_reason": pickaxe.get("reason"),
            "iron_ingot_reserved": inventory_count(body, "iron_ingot"),
            "offhand": slot_item(body, 40),
            "mainhand": selected_item,
            "mainhand_slot": selected_slot,
        },
    }


def run_honest_inverses(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    # These checks intentionally remove only the target domain and verify that
    # the production ingress reports typed failure without inventing progress.
    command(rcon, "fill 8 70 -4 16 78 4 air", delay=0.0)
    command(rcon, "fill 8 69 -4 16 69 4 deepslate", delay=0.0)
    parts = make_parts(body, "honest inverse checks")
    results = {}
    for label, item_name in (
        ("flower_missing", "dandelion"),
        ("coal_missing", "coal"),
        ("iron_missing", "raw_iron"),
    ):
        # The positive chain intentionally leaves some flower/coal inventory.
        # Request one more than authoritative inventory so this inverse tests
        # a missing world target rather than the already-satisfied fast path.
        params = {
            "item": item_name,
            "count": inventory_count(body, item_name) + 1,
            "constraints": {"radius": 12},
        }
        result = invoke(parts, "collect_resource", params)
        if result.get("success") is True or result.get("reason") not in {"target_not_found", "candidate_targets_exhausted", "partial_candidate_targets_exhausted"}:
            raise AssertionError(f"{label} was not an honest collect inverse: {result}")
        results[label] = {"success": result.get("success"), "reason": result.get("reason"), "can_retry": result.get("canRetry")}

    entity = invoke(parts, "search_for_entity", {"entity_types": ["sheep"], "search_radius": 8, "timeout_s": 8})
    if entity.get("success") is True or entity.get("reason") != "search_entity_not_found":
        raise AssertionError(f"animal inverse was not honest: {entity}")
    results["animal_missing"] = {"success": entity.get("success"), "reason": entity.get("reason")}
    return results


def main() -> None:
    with connect_or_skip() as rcon:
        body = ScarpetBody(BOT, rcon)
        setup_fixture(rcon)
        reset_bot(rcon, body)
        flowers = run_flowers(rcon, body)
        animals = run_animals(rcon, body)
        production = run_production_chain(rcon, body)
        inverses = run_honest_inverses(rcon, body)
        print(json.dumps({"flowers": flowers, "animals": animals, "production": production, "inverses": inverses}, sort_keys=True))
        command(rcon, f"player {BOT} kill", delay=0.2)
        command(rcon, "forceload remove -16 -64 32 64", delay=0.0)


if __name__ == "__main__":
    main()
