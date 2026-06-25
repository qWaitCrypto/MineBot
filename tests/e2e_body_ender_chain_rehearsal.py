#!/usr/bin/env python3
"""Ender-dragon physical-capability chain rehearsal on a natural live world.

This is a Body-layer integration rehearsal, not an Agent planner.  The script
chooses a random natural surface area, then drives already-exposed Body
transactions in sequence.  RCON commands are limited to test shell duties:
server setup, random placement, fixture seeds, hostile targets, and cleanup.
"""

from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, FurnaceTransactions, InventoryTransactions, LifecycleTransactions
from minebot.contract import Action, BreakContext
from minebot.game import GovernancePolicy, RconClient, Region, ScarpetBody
from minebot.game.rcon import RconConfig
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "E2EEnderChainBot"
BASE_MIN = -128
BASE_MAX = 128


def command(rcon: RconClient, command_text: str, delay: float = 0.05) -> str:
    out = rcon.command(command_text)
    if delay:
        time.sleep(delay)
    return out


def setup_server(rcon: RconClient) -> None:
    for cmd in [
        "script resume",
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "weather clear",
        "difficulty normal",
        "time set day",
        "kill @e[type=!player,tag=minebot_ender_chain_fixture]",
        "kill @e[type=arrow,tag=minebot_ender_chain_fixture]",
        f"player {BOT} kill",
        "script in minebot run minebot_reset()",
        "script in minebot run global_reflex_scan = false",
    ]:
        command(rcon, cmd)


def random_surface_start(rcon: RconClient, body: ScarpetBody) -> tuple[int, int, int]:
    """Place the bot in a random real terrain area, then read its surface block."""

    for attempt in range(10):
        x = random.randint(BASE_MIN, BASE_MAX)
        z = random.randint(BASE_MIN, BASE_MAX)
        command(rcon, f"tp {BOT} {x} 180 {z} 0 0", delay=0.4)
        stand = find_natural_stand_at(body, x, z)
        if stand is None:
            continue
        command(rcon, f"tp {BOT} {stand[0] + 0.5} {stand[1]} {stand[2] + 0.5} 0 0", delay=0.2)
        if is_dry_stand_cell(rcon, stand[0], stand[1], stand[2]):
            return stand
    raise AssertionError("could not find a random dry natural start for rehearsal")


def find_natural_stand_at(body: ScarpetBody, x: int, z: int) -> tuple[int, int, int] | None:
    for y in range(150, 50, -1):
        feet = block_fact(body, (x, y, z))
        head = block_fact(body, (x, y + 1, z))
        below = block_fact(body, (x, y - 1, z))
        feet_state = str(feet.get("state") or "")
        head_state = str(head.get("state") or "")
        below_state = str(below.get("state") or "")
        below_type = normalize_block_type(below.get("type"))
        if (
            feet_state == "CLEAR"
            and head_state == "CLEAR"
            and below_state not in {"CLEAR", "LIQUID"}
            and not below_type.endswith("_leaves")
            and below_type not in {"vine", "fire", "cactus", "magma_block"}
        ):
            return (x, y, z)
    return None


def is_dry_stand_cell(rcon: RconClient, x: int, y: int, z: int) -> bool:
    raw = command(rcon, f"script in minebot run is_dry_stand_cell({x},{y},{z})", delay=0.0)
    return "true" in raw


def normalize_block_type(block_type: object) -> str:
    return str(block_type or "unknown").removeprefix("minecraft:")


def block_fact(body: ScarpetBody, pos: tuple[int, int, int]) -> dict[str, object]:
    perception = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    if not perception.ok:
        raise AssertionError(f"blockAt failed at {pos}: {perception}")
    return dict(perception.data)


def make_body_runtimes(body: ScarpetBody, origin: tuple[int, int, int]):
    region = Region(
        "ender-chain-random-natural",
        (origin[0] - 48, 0, origin[2] - 48),
        (origin[0] + 48, 320, origin[2] + 48),
    )
    protected = Region(
        "ender-chain-protected-proof",
        (origin[0] + 2, origin[1] - 2, origin[2] + 2),
        (origin[0] + 2, origin[1] + 2, origin[2] + 2),
    )
    policy = GovernancePolicy(natural_regions=[region], protected_regions=[protected])
    work = BlockWork(body, policy)
    inventory = InventoryTransactions(body, governance=policy, work=work)
    furnace = FurnaceTransactions(body, governance=policy, work=work)
    return policy, work, inventory, furnace


def inventory_count(body: ScarpetBody, item: str) -> int:
    wanted = normalize_block_type(item)
    total = 0
    for slot in body.get_inventory():
        if slot.item is not None and normalize_block_type(slot.item) == wanted:
            total += slot.count
    return total


def read_target_uuid(rcon: RconClient, selector: str) -> str:
    raw = command(rcon, f"script in minebot run query(entity_selector('{selector}'):0, 'uuid')", delay=0.0)
    value = raw.strip()
    if "=" in value:
        value = value.split("=", 1)[1].strip()
    if " " in value:
        value = value.split(" ", 1)[0].strip()
    if not value or "No entity was found" in value or "Cannot" in value:
        raise AssertionError(f"could not read target uuid for {selector}: {raw!r}")
    return value


def set_slot(rcon: RconClient, slot: int, item: str | None, count: int = 1) -> None:
    if item is None:
        command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
        return
    command(rcon, f"script in minebot run inventory_set('{BOT}', {slot}, {count}, '{item}')")


def seed_rehearsal_inventory(rcon: RconClient) -> None:
    command(rcon, f"clear {BOT}")
    for slot in range(46):
        set_slot(rcon, slot, None)
    set_slot(rcon, 0, "minecraft:diamond_pickaxe", 1)
    set_slot(rcon, 1, "minecraft:oak_log", 3)
    set_slot(rcon, 2, "minecraft:raw_iron", 1)
    set_slot(rcon, 3, "minecraft:coal", 2)
    set_slot(rcon, 4, "minecraft:furnace", 1)
    set_slot(rcon, 5, "minecraft:crafting_table", 1)
    set_slot(rcon, 6, "minecraft:iron_helmet", 1)
    set_slot(rcon, 7, "minecraft:flint_and_steel", 1)
    set_slot(rcon, 8, "minecraft:bow", 1)
    set_slot(rcon, 40, "minecraft:arrow", 16)


def seed_tool_only_inventory(rcon: RconClient) -> None:
    command(rcon, f"clear {BOT}")
    for slot in range(46):
        set_slot(rcon, slot, None)
    set_slot(rcon, 0, "minecraft:diamond_pickaxe", 1)


def require_success(label: str, result) -> dict[str, object]:
    payload = result.to_payload()
    if not result.success:
        raise AssertionError(f"{label} failed: {payload}")
    return payload


def run_navigation_probe(body: ScarpetBody, origin: tuple[int, int, int]) -> dict[str, object]:
    candidates = [
        (origin[0] + dx, origin[1] + dy, origin[2] + dz)
        for dx in range(-4, 5)
        for dz in range(-4, 5)
        for dy in range(-1, 2)
        if abs(dx) + abs(dz) >= 3
    ]
    for candidate in candidates:
        feet = block_fact(body, candidate)
        head = block_fact(body, (candidate[0], candidate[1] + 1, candidate[2]))
        below = block_fact(body, (candidate[0], candidate[1] - 1, candidate[2]))
        if str(feet.get("state")) == "CLEAR" and str(head.get("state")) == "CLEAR" and str(below.get("state")) not in {"CLEAR", "LIQUID"}:
            action = Action.create("moveTo", {"target": [candidate[0], candidate[1], candidate[2]], "timeout_ticks": 140})
            accepted = body.execute(action)
            if not accepted.ok or not accepted.accepted:
                continue
            terminal = body.await_action_terminal(action.id, timeout_s=10.0)
            if terminal.name != "moveDone":
                continue
            final = terminal.data.get("final_pos") or []
            moved = False
            if len(final) == 3:
                moved = math.dist(tuple(final), (origin[0] + 0.5, origin[1], origin[2] + 0.5)) >= 1.0
            if terminal.data.get("arrived") or moved:
                return {"event": terminal.name, **terminal.data}
    raise RuntimeError("natural world did not expose a reachable local navigation candidate")


def choose_rehearsal_site(rcon: RconClient, body: ScarpetBody):
    failures: list[str] = []
    for _ in range(8):
        origin = random_surface_start(rcon, body)
        try:
            navigation = run_navigation_probe(body, origin)
        except RuntimeError as exc:
            failures.append(f"{origin}: {exc}")
            continue
        policy, work, inventory, furnace = make_body_runtimes(body, origin)
        return origin, policy, work, inventory, furnace, navigation
    raise AssertionError(f"could not find a random natural rehearsal site with reachable local navigation: {failures}")


def run_collect_and_governance(
    rcon: RconClient,
    body: ScarpetBody,
    work: BlockWork,
    origin: tuple[int, int, int],
) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()", delay=0.1)
    command(rcon, f"fill {origin[0] - 8} {origin[1] - 2} {origin[2] - 8} {origin[0] + 8} {origin[1] + 4} {origin[2] + 8} air replace fire")
    command(rcon, f"fill {origin[0] - 8} {origin[1] - 2} {origin[2] - 8} {origin[0] + 8} {origin[1] + 4} {origin[2] + 8} air replace lava")
    command(rcon, f"tp {BOT} {origin[0] + 0.5} {origin[1]} {origin[2] + 0.5} 0 0", delay=0.2)
    target = (origin[0] + 1, origin[1], origin[2])
    command(rcon, f"setblock {target[0]} {target[1]} {target[2]} dirt")
    protected = (origin[0] + 2, origin[1] - 1, origin[2] + 2)
    before = block_fact(body, target)
    command(rcon, f"setblock {protected[0]} {protected[1]} {protected[2]} stone")
    denied = work.mine_block(protected, context=BreakContext.TRAVEL, timeout_s=8.0)
    if denied.success or not denied.reason.startswith("break_denied"):
        raise AssertionError(f"governance inverse did not refuse protected break: {denied.to_payload()}")
    protected_after = block_fact(body, protected)
    if str(protected_after.get("state")) == "CLEAR":
        raise AssertionError(f"governance inverse mutated protected block: {protected_after}")

    result = work.mine_block(
        target,
        context=BreakContext.TRAVEL,
        timeout_s=12.0,
    )
    payload = require_success("natural mine", result)
    after = block_fact(body, target)
    if str(after.get("state")) != "CLEAR":
        raise AssertionError(f"natural mine did not clear target: before={before} after={after} result={payload}")
    return {
        "mine": payload,
        "target_before": before,
        "target_after": after,
        "governance_denied": denied.to_payload(),
        "protected_after": protected_after,
    }


def first_reachable_mine_target(body: ScarpetBody, origin: tuple[int, int, int]) -> tuple[int, int, int]:
    candidates = [
        (origin[0] + dx, origin[1] - 1, origin[2] + dz)
        for radius in (1, 2)
        for dx in range(-radius, radius + 1)
        for dz in range(-radius, radius + 1)
        if max(abs(dx), abs(dz)) == radius
        if not (dx == 0 and dz == 0)
    ]
    for candidate in candidates:
        fact = block_fact(body, candidate)
        block_type = normalize_block_type(fact.get("type"))
        if str(fact.get("state")) == "CLEAR" or block_type in {"water", "lava"}:
            continue
        return candidate
    raise AssertionError(f"no directly reachable natural mine target near {origin}")


def first_mine_stand_point(body: ScarpetBody, target: tuple[int, int, int]) -> tuple[int, int, int] | None:
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        stand = (target[0] + dx, target[1] + 1, target[2] + dz)
        feet = block_fact(body, stand)
        head = block_fact(body, (stand[0], stand[1] + 1, stand[2]))
        below = block_fact(body, (stand[0], stand[1] - 1, stand[2]))
        if (
            str(feet.get("state")) == "CLEAR"
            and str(head.get("state")) == "CLEAR"
            and str(below.get("state")) not in {"CLEAR", "LIQUID"}
        ):
            return stand
    return None


def run_craft_smelt_equip(
    rcon: RconClient,
    body: ScarpetBody,
    origin: tuple[int, int, int],
    work: BlockWork,
    inventory: InventoryTransactions,
    furnace: FurnaceTransactions,
) -> dict[str, object]:
    craft = inventory.craft_recipe(
        item="minecraft:oak_planks",
        count=4,
        output_slot=9,
        craft_timeout_s=6.0,
        approach_timeout_s=12.0,
        place_timeout_s=12.0,
    )
    craft_payload = require_success("craft oak_planks", craft)

    command(rcon, f"tp {BOT} {origin[0] + 0.5} {origin[1]} {origin[2] + 0.5} 0 0", delay=0.2)
    furnace_pos = (origin[0] + 1, origin[1], origin[2])
    command(rcon, f"setblock {furnace_pos[0]} {furnace_pos[1] - 1} {furnace_pos[2]} stone")
    command(rcon, f"setblock {furnace_pos[0]} {furnace_pos[1]} {furnace_pos[2]} air")
    smelt = furnace.smelt_with_temporary_furnace(
        furnace_pos,
        input_item="minecraft:raw_iron",
        input_count=1,
        fuel_item="minecraft:coal",
        fuel_count=1,
        output_item="minecraft:iron_ingot",
        output_count=1,
        output_slot=10,
        smelt_timeout_s=20.0,
        place_timeout_s=12.0,
        reclaim_timeout_s=12.0,
        reclaim_tool="minecraft:diamond_pickaxe",
    )
    smelt_payload = require_success("temporary furnace smelt", smelt)

    equip = inventory.equip_item(item="minecraft:iron_helmet", target="head", timeout_s=4.0)
    equip_payload = require_success("equip helmet", equip)
    if inventory_count(body, "iron_ingot") < 1:
        raise AssertionError("smelt chain did not leave an iron_ingot in inventory")
    _ = rcon
    _ = work
    return {"craft": craft_payload, "smelt": smelt_payload, "equip": equip_payload}


def first_supported_clear_target(
    body: ScarpetBody,
    origin: tuple[int, int, int],
    *,
    min_radius: int,
    max_radius: int,
) -> tuple[int, int, int]:
    for radius in range(min_radius, max_radius + 1):
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if max(abs(dx), abs(dz)) != radius:
                    continue
                target = (origin[0] + dx, origin[1], origin[2] + dz)
                feet = block_fact(body, target)
                head = block_fact(body, (target[0], target[1] + 1, target[2]))
                below = block_fact(body, (target[0], target[1] - 1, target[2]))
                if (
                    str(feet.get("state")) == "CLEAR"
                    and str(head.get("state")) == "CLEAR"
                    and str(below.get("state")) not in {"CLEAR", "LIQUID"}
                ):
                    return target
    raise AssertionError(f"no supported clear target near natural origin {origin}")


def run_use_and_combat(rcon: RconClient, body: ScarpetBody, origin: tuple[int, int, int]) -> dict[str, object]:
    command(rcon, f"tp {BOT} {origin[0] + 0.5} {origin[1]} {origin[2] + 0.5} 0 0", delay=0.2)
    fire_target = (origin[0] + 3, origin[1], origin[2])
    command(rcon, f"setblock {fire_target[0]} {fire_target[1] - 1} {fire_target[2]} netherrack")
    command(rcon, f"setblock {fire_target[0]} {fire_target[1]} {fire_target[2]} air")
    command(rcon, f"item replace entity {BOT} hotbar.7 with flint_and_steel")
    command(rcon, f"player {BOT} hotbar 8")
    ignite = body.ignite_block(fire_target, item="minecraft:flint_and_steel", allow_server_substitute=True, timeout_s=8.0)
    if ignite.name != "igniteDone" or not ignite.data.get("success"):
        raise AssertionError(f"igniteBlock did not complete: {ignite}")
    fire = block_fact(body, fire_target)
    if normalize_block_type(fire.get("type")) != "fire":
        raise AssertionError(f"igniteBlock lacked authoritative fire truth: event={ignite} block={fire}")

    command(rcon, f"setblock {fire_target[0]} {fire_target[1]} {fire_target[2]} air", delay=0.1)
    command(rcon, "script in minebot run minebot_reset()", delay=0.1)
    command(rcon, f"tp {BOT} {origin[0] + 0.5} {origin[1]} {origin[2] + 0.5} 0 0", delay=0.2)
    melee_pos = (origin[0] + 2, origin[1], origin[2])
    command(
        rcon,
        f'summon husk {melee_pos[0]} {melee_pos[1]} {melee_pos[2]} '
        '{NoAI:1b,NoGravity:1b,Health:12f,PersistenceRequired:1b,Tags:["minebot_ender_chain_fixture"]}',
    )
    command(rcon, f"item replace entity {BOT} hotbar.0 with diamond_sword")
    command(rcon, f"player {BOT} hotbar 1")
    melee = body.attack_entity(target_type="minecraft:husk", radius=8, timeout_ticks=160, cooldown_ticks=8, timeout_s=12.0)
    if melee.name != "attackDone" or not (melee.data.get("stopped_reason") == "killed" or melee.data.get("damage_observed")):
        raise AssertionError(f"melee combat did not kill target truthfully: {melee}")

    command(rcon, "kill @e[type=husk,tag=minebot_ender_chain_fixture]", delay=0.1)
    command(rcon, "kill @e[type=arrow]", delay=0.1)
    command(rcon, "script in minebot run minebot_reset()", delay=0.1)
    command(rcon, "script in minebot run global_reflex_scan = false", delay=0.0)
    command(rcon, f"tp {BOT} {origin[0] + 0.5} {origin[1]} {origin[2] + 0.5} 0 0", delay=0.2)
    ranged_pos = (origin[0], origin[1], origin[2] + 6)
    command(
        rcon,
        f'summon husk {ranged_pos[0]} {ranged_pos[1]} {ranged_pos[2]} '
        '{NoAI:1b,NoGravity:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_ender_chain_fixture"]}',
    )
    target_id = read_target_uuid(rcon, "@e[type=husk,tag=minebot_ender_chain_fixture,limit=1]")
    command(rcon, f"item replace entity {BOT} hotbar.8 with bow")
    command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
    command(rcon, f"player {BOT} hotbar 9")
    ranged = body.ranged_attack(
        weapon="bow",
        target_type="minecraft:husk",
        target_id=target_id,
        radius=16,
        timeout_ticks=120,
        use_interval_ticks=22,
        expected_shots=1,
        timeout_s=12.0,
    )
    if ranged.name != "rangedDone" or not (ranged.data.get("damage_observed") or ranged.data.get("fired_observed")):
        raise AssertionError(f"ranged combat did not fire or observe damage truth: {ranged}")
    if ranged.data.get("target_id") != target_id:
        raise AssertionError(f"ranged combat hit wrong target: expected={target_id} event={ranged.data}")
    return {"ignite": ignite.data, "melee": melee.data, "ranged": ranged.data}


def wait_for_events(body: ScarpetBody, names: set[str], *, timeout_s: float = 12.0):
    deadline = time.monotonic() + timeout_s
    seen = {}
    while time.monotonic() < deadline:
        for event in body.poll_events():
            if event.name in names and event.name not in seen:
                seen[event.name] = event
        if all(name in seen for name in names):
            return seen
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for events: {sorted(names - set(seen))}")


def wait_for_escaped_reflex(body: ScarpetBody, *, timeout_s: float = 12.0):
    deadline = time.monotonic() + timeout_s
    seen_triggered = None
    completions = []
    while time.monotonic() < deadline:
        for event in body.poll_events():
            if event.name == "reflexTriggered" and seen_triggered is None:
                seen_triggered = event
            if event.name == "reflexCompleted":
                completions.append(event)
                if event.data.get("escaped_hazard") is True:
                    return seen_triggered, event, completions
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for escaped reflex: triggered={seen_triggered} completions={completions}")


def wait_for_missing_body(body: ScarpetBody, *, timeout_s: float = 12.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    events = []
    while time.monotonic() < deadline:
        events.extend(event for event in body.poll_events() if event.name in {"death", "bodyMissing"})
        state = body.get_state()
        if state.missing:
            return {"events": [event.data for event in events], "state": {"missing": state.missing, "pos": list(state.pos)}}
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for missing body: events={events}")


def run_survival_and_death(rcon: RconClient, body: ScarpetBody, origin: tuple[int, int, int]) -> dict[str, object]:
    command(rcon, "script in minebot run minebot_reset()", delay=0.1)
    lava_pos = (origin[0], origin[1], origin[2])
    command(rcon, f"fill {origin[0] - 1} {origin[1]} {origin[2] - 1} {origin[0] + 4} {origin[1] + 1} {origin[2] + 1} air")
    command(rcon, f"fill {origin[0] - 1} {origin[1] - 1} {origin[2] - 1} {origin[0] + 4} {origin[1] - 1} {origin[2] + 1} stone")
    command(rcon, f"setblock {lava_pos[0]} {lava_pos[1]} {lava_pos[2]} lava")
    command(rcon, f"tp {BOT} {origin[0] + 0.5} {origin[1]} {origin[2] + 0.5} -90 0", delay=0.2)
    raw = command(rcon, f"script in minebot run start_lava_reflex('{BOT}')", delay=0.2)
    if "true" not in raw:
        raise AssertionError(f"lava reflex did not start: {raw}")
    triggered_events = wait_for_events(body, {"reflexTriggered"}, timeout_s=4.0)
    triggered = triggered_events["reflexTriggered"]
    command(rcon, "script in minebot run minebot_reset()", delay=0.1)

    lifecycle = LifecycleTransactions(body)
    command(rcon, f"player {BOT} kill", delay=0.5)
    missing = wait_for_missing_body(body, timeout_s=12.0)
    recovered = lifecycle.recover_after_death(respawn_pos=origin, yaw=90.0, pitch=0.0)
    recovered_payload = require_success("recover_after_death", recovered)
    command(rcon, "script in minebot run minebot_reset()", delay=0.1)
    command(rcon, "script in minebot run global_reflex_scan = false", delay=0.0)
    continue_action = Action.create("moveTo", {"target": [origin[0] + 1, origin[1], origin[2]], "timeout_ticks": 100})
    accepted = body.execute(continue_action)
    if not accepted.ok or not accepted.accepted:
        raise AssertionError(f"post-recovery moveTo was not accepted: {accepted}")
    terminal = body.await_action_terminal(continue_action.id, timeout_s=8.0)
    if terminal.name != "moveDone" or not terminal.data.get("arrived"):
        raise AssertionError(f"post-recovery moveTo did not arrive: {terminal}")
    return {
        "triggered": triggered.data,
        "missing": missing,
        "recovery": recovered_payload,
        "post_recovery": terminal.data,
    }


def main() -> int:
    with connect_or_skip(RconConfig(timeout_s=60.0)) as rcon:
        setup_server(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (0, 80, 0), timeout_s=15.0)
        command(rcon, f"gamemode survival {BOT}")
        origin, policy, work, inventory, furnace, navigation = choose_rehearsal_site(rcon, body)
        command(rcon, f"tp {BOT} {origin[0] + 0.5} {origin[1]} {origin[2] + 0.5} 0 0", delay=0.2)
        seed_tool_only_inventory(rcon)
        command(rcon, "script in minebot run minebot_reset()")
        _ = policy

        collect_governance = run_collect_and_governance(rcon, body, work, origin)
        seed_rehearsal_inventory(rcon)
        command(rcon, "script in minebot run minebot_reset()")

        evidence = {
            "origin": origin,
            "navigation": navigation,
            "collect_governance": collect_governance,
            "craft_smelt_equip": run_craft_smelt_equip(rcon, body, origin, work, inventory, furnace),
            "use_combat": run_use_and_combat(rcon, body, origin),
            "survival_death": run_survival_and_death(rcon, body, origin),
            "observability": body.observability_snapshot(),
        }
        print(evidence)
        command(rcon, "kill @e[type=!player,tag=minebot_ender_chain_fixture]", delay=0.0)
        command(rcon, "kill @e[type=arrow]", delay=0.0)
        command(rcon, f"player {BOT} kill", delay=0.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
