#!/usr/bin/env python3
"""Live reflex-preemption e2e for use/combat/ranged action controllers."""

from __future__ import annotations

import sys
import time
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import Action, ScarpetBody
from minebot.contract import terminal_event_to_tool_result
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "E2EReflexBot"
TARGET = "E2EReflexTarget"
BASE = (300, 60, 0)


def command(rcon, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon) -> None:
    for cmd in [
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doMobSpawning false",
        "gamerule doWeatherCycle false",
        "weather clear",
        "difficulty normal",
        "time set day",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        f"player {TARGET} kill",
        "script in minebot run minebot_reset()",
        "script in minebot run global_reflex_scan = false",
    ]:
        command(rcon, cmd)


def wait_for_event(body: ScarpetBody, name: str, *, timeout_s: float = 8.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in body.poll_events():
            if event.name == name:
                return event
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for event {name}")


def arm_lava_reflex_fixture(rcon) -> None:
    x, y, z = BASE
    command(rcon, f"fill {x-2} {y} {z-2} {x+16} {y+6} {z+3} air")
    command(rcon, f"fill {x-2} {y-1} {z-2} {x+16} {y-1} {z+3} stone")
    command(rcon, f"setblock {x+1} {y-1} {z} lava")
    command(rcon, f"tp {BOT} {x} {y} {z} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"player {BOT} stop")


def arm_lava_reflex_blocked_first_candidate_fixture(rcon) -> None:
    x, y, z = BASE
    command(rcon, f"fill {x-2} {y-1} {z-2} {x+2} {y+3} {z+2} air")
    command(rcon, f"fill {x-2} {y-1} {z-2} {x+2} {y-1} {z+2} stone")
    command(rcon, f"fill {x-2} {y} {z-2} {x-2} {y+2} {z+2} stone")
    command(rcon, f"fill {x+2} {y} {z-2} {x+2} {y+2} {z+2} stone")
    command(rcon, f"fill {x-1} {y} {z-2} {x+1} {y+2} {z-2} stone")
    command(rcon, f"fill {x-1} {y} {z+2} {x+1} {y+2} {z+2} stone")
    command(rcon, f"setblock {x+1} {y-1} {z} lava")
    command(rcon, f"setblock {x+1} {y} {z} gravel")
    command(rcon, f"tp {BOT} {x} {y} {z} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"player {BOT} stop")


def arm_persistent_lava_reflex_fixture(rcon) -> None:
    x, y, z = BASE
    command(rcon, f"fill {x-2} {y} {z-2} {x+16} {y+6} {z+3} air")
    command(rcon, f"fill {x-2} {y-1} {z-2} {x+16} {y-1} {z+3} stone")
    command(rcon, f"setblock {x+1} {y-1} {z} lava")
    command(rcon, f"setblock {x+1} {y} {z} stone")
    command(rcon, f"tp {BOT} {x} {y} {z} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"player {BOT} stop")


def arm_fire_reflex_fixture(rcon) -> None:
    x, y, z = BASE
    command(rcon, f"fill {x-2} {y} {z-2} {x+16} {y+6} {z+3} air")
    command(rcon, f"fill {x-2} {y-1} {z-2} {x+16} {y-1} {z+3} stone")
    command(rcon, f"setblock {x} {y-1} {z} netherrack")
    command(rcon, f"setblock {x} {y} {z} fire")
    command(rcon, f"tp {BOT} {x} {y} {z} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"player {BOT} stop")


def arm_water_reflex_fixture(rcon) -> None:
    x, y, z = BASE
    command(rcon, f"fill {x-3} {y} {z-3} {x+16} {y+6} {z+3} air")
    command(rcon, f"fill {x-3} {y-1} {z-3} {x+16} {y-1} {z+3} stone")
    command(rcon, f"fill {x-1} {y} {z-1} {x+4} {y+2} {z-1} stone")
    command(rcon, f"fill {x-1} {y} {z+1} {x+4} {y+2} {z+1} stone")
    command(rcon, f"setblock {x-1} {y} {z} stone")
    command(rcon, f"setblock {x-1} {y+1} {z} stone")
    command(rcon, f"fill {x+1} {y} {z} {x+4} {y+1} {z} stone")
    command(rcon, f"setblock {x} {y} {z} water")
    command(rcon, f"setblock {x} {y+1} {z} water")
    command(rcon, f"tp {BOT} {x} {y} {z} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"player {BOT} stop")
    command(rcon, "script in minebot run global_water_reflex_air_threshold = 295")
    command(rcon, "script in minebot run global_water_reflex_damage_budget = null")


def arm_trapped_water_reflex_fixture(rcon) -> None:
    x, y, z = BASE
    command(rcon, f"fill {x-3} {y-1} {z-3} {x+3} {y+5} {z+3} stone")
    command(rcon, f"fill {x} {y} {z} {x} {y+2} {z} water")
    command(rcon, f"tp {BOT} {x} {y} {z} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"player {BOT} stop")
    command(rcon, "script in minebot run global_water_reflex_air_threshold = 295")
    command(rcon, "script in minebot run global_water_reflex_damage_budget = null")


def arm_lava_no_escape_fixture(rcon) -> None:
    x, y, z = BASE
    command(rcon, f"fill {x-3} {y-1} {z-3} {x+3} {y+3} {z+3} air")
    command(rcon, f"setblock {x} {y-1} {z} stone")
    command(rcon, f"fill {x-2} {y} {z} {x+2} {y+1} {z} lava")
    command(rcon, f"fill {x} {y} {z-2} {x} {y+1} {z+2} lava")
    command(rcon, f"setblock {x} {y} {z} air")
    command(rcon, f"setblock {x} {y+1} {z} air")
    command(rcon, f"tp {BOT} {x} {y} {z} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect give {BOT} minecraft:fire_resistance 20 0 true")
    command(rcon, f"player {BOT} stop")


def assert_neutral_preempted(body: ScarpetBody, terminal, *, action_name: str, expect_event: str, event_start: int) -> None:
    result = terminal_event_to_tool_result(terminal)
    payload = result.to_payload()
    if not result.success or result.reason != "preempted" or not result.can_retry:
        raise AssertionError(f"{action_name} was not neutrally preempted: {payload} terminal={terminal.data}")
    metrics = result.metrics or {}
    if metrics.get("paused") is not True:
        raise AssertionError(f"{action_name} preemption did not expose paused sentinel: {payload}")
    if terminal.name != expect_event:
        raise AssertionError(f"{action_name} returned wrong terminal event: {terminal}")
    if terminal.data.get("stopped_reason") != "preempted":
        raise AssertionError(f"{action_name} terminal event lost preempted reason: {terminal.data}")
    recent_events = body.event_log[event_start:]
    triggered = next((event for event in recent_events if event.name == "reflexTriggered"), None)
    if triggered is None:
        triggered = wait_for_event(body, "reflexTriggered", timeout_s=3.0)
        recent_events = body.event_log[event_start:]
    completed = next((event for event in recent_events if event.name == "reflexCompleted"), None)
    if completed is None:
        completed = wait_for_event(body, "reflexCompleted", timeout_s=8.0)
        recent_events = body.event_log[event_start:]
    owner_preempted = next((event for event in recent_events if event.name == "ownerPreempted"), None)
    if owner_preempted is None:
        owner_preempted = wait_for_event(body, "ownerPreempted", timeout_s=3.0)
    reflex_owner = triggered.data.get("kind", "lava") + "Reflex"
    if owner_preempted.data.get("previous_owner") != action_name or owner_preempted.data.get("new_owner") != reflex_owner:
        raise AssertionError(f"{action_name} owner handoff drifted: {owner_preempted.data}")
    if completed.data.get("escaped_hazard") is not True:
        raise AssertionError(f"{action_name} reflex did not escape hazard: {completed.data}")


def trigger_lava_reflex(rcon) -> None:
    raw = command(rcon, f"script in minebot run start_lava_reflex('{BOT}')", delay=0.2)
    if " = true" not in raw and raw.strip() != "true":
        raise AssertionError(f"manual lava reflex trigger failed: {raw}")


def trigger_fire_reflex(rcon) -> None:
    raw = command(rcon, f"script in minebot run start_fire_reflex('{BOT}')", delay=0.2)
    if " = true" not in raw and raw.strip() != "true":
        raise AssertionError(f"manual fire reflex trigger failed: {raw}")


def trigger_water_reflex(rcon, *, expect_started: bool = True) -> str:
    raw = command(rcon, f"script in minebot run start_water_reflex('{BOT}')", delay=0.2)
    started = " = true" in raw or raw.strip() == "true"
    if expect_started and not started:
        raise AssertionError(f"manual water reflex trigger failed: {raw}")
    return raw


def set_reflex_scan(rcon, enabled: bool) -> None:
    value = "true" if enabled else "false"
    raw = command(rcon, f"script in minebot run global_reflex_scan = {value}", delay=0.1)
    if value not in raw and raw.strip() != value:
        raise AssertionError(f"failed to set global_reflex_scan={value}: {raw}")


def assert_post_reflex_continuation(body: ScarpetBody, *, target: tuple[int, int, int]) -> None:
    action = Action.create("moveTo", {"target": list(target)})
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"post-reflex continuation moveTo was not accepted: {result}")
    terminal = body.await_action_terminal(action.id, timeout_s=8.0)
    if terminal.name != "moveDone" or not terminal.data.get("arrived"):
        raise AssertionError(f"post-reflex continuation moveTo did not complete truthfully: {terminal}")
    final = body.get_state()
    dx = final.pos[0] - target[0]
    dy = final.pos[1] - target[1]
    dz = final.pos[2] - target[2]
    if (dx * dx + dy * dy + dz * dz) ** 0.5 > 1.5:
        raise AssertionError(f"post-reflex continuation landed too far from target: final={final.pos} target={target}")


def assert_post_reflex_stop_release(body: ScarpetBody) -> None:
    action = Action.create("stop", {})
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"post-reflex stop was not accepted: {result}")
    terminal = body.await_action_terminal(action.id, timeout_s=5.0)
    if terminal.name != "stopDone" or not terminal.data.get("success"):
        raise AssertionError(f"post-reflex stop did not complete truthfully: {terminal}")


def assert_water_reflex_exited_to_dry_stand(rcon, body: ScarpetBody) -> None:
    final = body.get_state()
    x = math.floor(final.pos[0])
    y = math.floor(final.pos[1])
    z = math.floor(final.pos[2])
    raw = command(rcon, f"script in minebot run is_dry_stand_cell({x},{y},{z})", delay=0.0)
    if "true" not in raw:
        raise AssertionError(f"water reflex did not exit to dry stand: final={final.pos} oxygen={final.oxygen} raw={raw}")


def run_use_item_preempt(rcon, body: ScarpetBody) -> None:
    arm_lava_reflex_fixture(rcon)
    command(rcon, f"effect give {BOT} minecraft:hunger 30 255 true", delay=6.0)
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with bread 2")
    action = Action.create("useItem", {"mode": "continuous", "ticks": 80, "item": "minecraft:bread", "slot": 0})
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"useItem action was not accepted before reflex probe: {result}")
    trigger_lava_reflex(rcon)
    terminal = body.await_action_terminal(action.id, timeout_s=10.0)
    assert_neutral_preempted(body, terminal, action_name="useItem", expect_event="useDone", event_start=event_start)
    assert_post_reflex_continuation(body, target=(BASE[0] - 2, BASE[1], BASE[2]))


def run_attack_preempt(rcon, body: ScarpetBody) -> None:
    arm_lava_reflex_fixture(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with diamond_sword")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {BASE[0] + 14} {BASE[1] - 1} {BASE[2]} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_reflex_attack_target"]}',
    )
    action = Action.create(
        "attackEntity",
        {"target_type": "minecraft:husk", "radius": 24, "timeout_ticks": 160, "cooldown_ticks": 8},
    )
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"attackEntity action was not accepted before reflex probe: {result}")
    trigger_lava_reflex(rcon)
    terminal = body.await_action_terminal(action.id, timeout_s=10.0)
    assert_neutral_preempted(body, terminal, action_name="attackEntity", expect_event="attackDone", event_start=event_start)
    assert_post_reflex_continuation(body, target=(BASE[0] - 2, BASE[1], BASE[2]))


def run_ranged_preempt(rcon, body: ScarpetBody) -> None:
    arm_lava_reflex_fixture(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {BASE[0] + 14} {BASE[1] - 1} {BASE[2]} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_reflex_ranged_target"]}',
    )
    action = Action.create(
        "rangedAttack",
        {
            "weapon": "bow",
            "target_type": "minecraft:husk",
            "radius": 24,
            "timeout_ticks": 120,
            "use_interval_ticks": 22,
            "expected_shots": 1,
        },
    )
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"rangedAttack action was not accepted before reflex probe: {result}")
    trigger_lava_reflex(rcon)
    terminal = body.await_action_terminal(action.id, timeout_s=10.0)
    assert_neutral_preempted(body, terminal, action_name="rangedAttack", expect_event="rangedDone", event_start=event_start)
    assert_post_reflex_continuation(body, target=(BASE[0] - 2, BASE[1], BASE[2]))


def run_mine_preempt(rcon, body: ScarpetBody) -> None:
    arm_lava_reflex_fixture(rcon)
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    command(rcon, f"setblock {BASE[0] + 2} {BASE[1]} {BASE[2]} stone")
    action = Action.create(
        "mineBlock",
        {
            "target": [BASE[0] + 2, BASE[1], BASE[2]],
            "block_type": "minecraft:stone",
            "timeout_ticks": 180,
        },
    )
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"mineBlock action was not accepted before reflex probe: {result}")
    trigger_lava_reflex(rcon)
    terminal = body.await_action_terminal(action.id, timeout_s=10.0)
    assert_neutral_preempted(body, terminal, action_name="mineBlock", expect_event="mineDone", event_start=event_start)
    assert_post_reflex_continuation(body, target=(BASE[0] - 2, BASE[1], BASE[2]))


def run_place_preempt(rcon, body: ScarpetBody) -> None:
    arm_lava_reflex_fixture(rcon)
    command(rcon, f"item replace entity {BOT} weapon.mainhand with cobblestone 8")
    command(rcon, f"setblock {BASE[0] + 8} {BASE[1]} {BASE[2]} air")
    command(rcon, f"setblock {BASE[0] + 8} {BASE[1] - 1} {BASE[2]} stone")
    action = Action.create(
        "placeBlock",
        {
            "target": [BASE[0] + 8, BASE[1], BASE[2]],
            "block_type": "minecraft:cobblestone",
            "face": "up",
            "timeout_ticks": 120,
        },
    )
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"placeBlock action was not accepted before reflex probe: {result}")
    trigger_lava_reflex(rcon)
    terminal = body.await_action_terminal(action.id, timeout_s=10.0)
    assert_neutral_preempted(body, terminal, action_name="placeBlock", expect_event="placeDone", event_start=event_start)
    assert_post_reflex_continuation(body, target=(BASE[0] - 2, BASE[1], BASE[2]))


def run_auto_lava_preempt(rcon, body: ScarpetBody) -> None:
    arm_persistent_lava_reflex_fixture(rcon)
    set_reflex_scan(rcon, False)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {BASE[0] + 14} {BASE[1] - 1} {BASE[2]} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_reflex_auto_target"]}',
    )
    action = Action.create(
        "rangedAttack",
        {
            "weapon": "bow",
            "target_type": "minecraft:husk",
            "radius": 24,
            "timeout_ticks": 120,
            "use_interval_ticks": 22,
            "expected_shots": 1,
        },
    )
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"auto-scan rangedAttack action was not accepted before reflex probe: {result}")
    time.sleep(0.3)
    set_reflex_scan(rcon, True)
    terminal = body.await_action_terminal(action.id, timeout_s=10.0)
    assert_neutral_preempted(body, terminal, action_name="rangedAttack", expect_event="rangedDone", event_start=event_start)
    set_reflex_scan(rcon, False)
    assert_post_reflex_continuation(body, target=(BASE[0] - 2, BASE[1], BASE[2]))


def run_fire_ranged_preempt(rcon, body: ScarpetBody) -> None:
    arm_fire_reflex_fixture(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {BASE[0] + 14} {BASE[1] - 1} {BASE[2]} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_reflex_fire_target"]}',
    )
    action = Action.create(
        "rangedAttack",
        {
            "weapon": "bow",
            "target_type": "minecraft:husk",
            "radius": 24,
            "timeout_ticks": 120,
            "use_interval_ticks": 22,
            "expected_shots": 1,
        },
    )
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"fire reflex rangedAttack action was not accepted before reflex probe: {result}")
    trigger_fire_reflex(rcon)
    terminal = body.await_action_terminal(action.id, timeout_s=10.0)
    assert_neutral_preempted(body, terminal, action_name="rangedAttack", expect_event="rangedDone", event_start=event_start)
    assert_post_reflex_continuation(body, target=(BASE[0] - 2, BASE[1], BASE[2]))


def run_auto_water_ranged_preempt(rcon, body: ScarpetBody) -> None:
    arm_water_reflex_fixture(rcon)
    set_reflex_scan(rcon, False)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {BASE[0] + 14} {BASE[1] - 1} {BASE[2]} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_reflex_water_target"]}',
    )
    action = Action.create(
        "rangedAttack",
        {
            "weapon": "bow",
            "target_type": "minecraft:husk",
            "radius": 24,
            "timeout_ticks": 160,
            "use_interval_ticks": 22,
            "expected_shots": 1,
        },
    )
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"water rangedAttack action was not accepted before reflex probe: {result}")
    time.sleep(0.5)
    set_reflex_scan(rcon, True)
    terminal = body.await_action_terminal(action.id, timeout_s=12.0)
    assert_neutral_preempted(body, terminal, action_name="rangedAttack", expect_event="rangedDone", event_start=event_start)
    set_reflex_scan(rcon, False)
    assert_post_reflex_stop_release(body)


def run_water_entry_below_threshold_does_not_preempt(rcon, body: ScarpetBody) -> None:
    arm_water_reflex_fixture(rcon)
    command(rcon, "script in minebot run global_water_reflex_air_threshold = 80")
    command(rcon, "script in minebot run global_water_reflex_damage_budget = null")
    raw = command(rcon, f"script in minebot run water_reflex_should_trigger('{BOT}')", delay=0.0)
    if "false" not in raw:
        raise AssertionError(f"water entry should not trigger before oxygen/damage risk: {raw}")


def run_water_damage_budget_auto_preempt(rcon, body: ScarpetBody) -> None:
    arm_water_reflex_fixture(rcon)
    set_reflex_scan(rcon, False)
    command(rcon, "script in minebot run global_water_reflex_air_threshold = -1")
    command(rcon, "script in minebot run global_water_reflex_damage_budget = 1")
    command(rcon, f"script in minebot run global_water_reflex_health_baselines:'{BOT}' = null")
    baseline = command(rcon, f"script in minebot run water_reflex_should_trigger('{BOT}')", delay=0.0)
    if "false" not in baseline:
        raise AssertionError(f"water damage-budget baseline should not trigger before damage: {baseline}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {BASE[0] + 14} {BASE[1] - 1} {BASE[2]} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_reflex_water_damage_target"]}',
    )
    action = Action.create(
        "rangedAttack",
        {
            "weapon": "bow",
            "target_type": "minecraft:husk",
            "radius": 24,
            "timeout_ticks": 160,
            "use_interval_ticks": 22,
            "expected_shots": 1,
        },
    )
    event_start = len(body.event_log)
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"water damage-budget rangedAttack action was not accepted before reflex probe: {result}")
    command(rcon, f"damage {BOT} 2 minecraft:generic", delay=0.2)
    set_reflex_scan(rcon, True)
    terminal = body.await_action_terminal(action.id, timeout_s=12.0)
    assert_neutral_preempted(body, terminal, action_name="rangedAttack", expect_event="rangedDone", event_start=event_start)
    set_reflex_scan(rcon, False)
    assert_post_reflex_stop_release(body)


def run_water_reflex_honest_failure(rcon, body: ScarpetBody) -> None:
    arm_trapped_water_reflex_fixture(rcon)
    event_start = len(body.event_log)
    trigger_water_reflex(rcon, expect_started=False)
    recent_events = body.event_log[event_start:]
    completed = next((event for event in recent_events if event.name == "reflexCompleted"), None)
    if completed is None:
        completed = wait_for_event(body, "reflexCompleted", timeout_s=3.0)
    if completed.data.get("kind") != "water" or completed.data.get("escaped_hazard") is not False:
        raise AssertionError(f"trapped water reflex should fail honestly, got: {completed.data}")
    assert_post_reflex_stop_release(body)


def run_lava_reflex_honest_failure_releases_owner(rcon, body: ScarpetBody) -> None:
    arm_lava_no_escape_fixture(rcon)
    event_start = len(body.event_log)
    raw = command(rcon, f"script in minebot run start_lava_reflex('{BOT}')", delay=0.2)
    if "false" not in raw:
        raise AssertionError(f"lava no-escape reflex should fail to start with honest completion: {raw}")
    recent_events = body.event_log[event_start:]
    completed = next((event for event in recent_events if event.name == "reflexCompleted"), None)
    if completed is None:
        completed = wait_for_event(body, "reflexCompleted", timeout_s=3.0)
    if completed.data.get("kind") != "lava" or completed.data.get("escaped_hazard") is not False:
        raise AssertionError(f"lava no-escape reflex should fail honestly, got: {completed.data}")
    assert_post_reflex_stop_release(body)


def run_lava_reflex_skips_non_standable_candidate(rcon, body: ScarpetBody) -> None:
    arm_lava_reflex_blocked_first_candidate_fixture(rcon)
    event_start = len(body.event_log)
    trigger_lava_reflex(rcon)
    recent_events = body.event_log[event_start:]
    triggered = next((event for event in recent_events if event.name == "reflexTriggered"), None)
    if triggered is None:
        triggered = wait_for_event(body, "reflexTriggered", timeout_s=3.0)
    if triggered.data.get("kind") != "lava" or triggered.data.get("target_is_dry_stand") is not True:
        raise AssertionError(f"lava reflex selected a non-standable target: {triggered.data}")
    target = triggered.data.get("target")
    if not isinstance(target, list) or len(target) != 3:
        raise AssertionError(f"lava reflex target is not a position: {triggered.data}")
    target_cell = tuple(math.floor(float(axis)) for axis in target)
    expected = (BASE[0] - 1, BASE[1], BASE[2])
    if target_cell != expected:
        raise AssertionError(f"lava reflex did not skip blocked first candidate: target={target} expected={expected}")
    recent_events = body.event_log[event_start:]
    completed = next((event for event in recent_events if event.name == "reflexCompleted"), None)
    if completed is None:
        completed = wait_for_event(body, "reflexCompleted", timeout_s=8.0)
    if completed.data.get("escaped_hazard") is not True or completed.data.get("final_is_dry_stand") is not True:
        raise AssertionError(f"lava reflex did not settle on dry ground: {completed.data}")
    final = body.get_state()
    x, y, z = (math.floor(axis) for axis in final.pos)
    raw = command(rcon, f"script in minebot run is_dry_stand_cell({x},{y},{z})", delay=0.0)
    if "true" not in raw:
        raise AssertionError(f"lava reflex final position is not a dry stand: final={final.pos} raw={raw}")


def main() -> int:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        try:
            spawn_or_fail(body, BASE)
            run_use_item_preempt(rcon, body)
            run_attack_preempt(rcon, body)
            run_ranged_preempt(rcon, body)
            run_mine_preempt(rcon, body)
            run_place_preempt(rcon, body)
            run_auto_lava_preempt(rcon, body)
            run_fire_ranged_preempt(rcon, body)
            run_water_entry_below_threshold_does_not_preempt(rcon, body)
            run_auto_water_ranged_preempt(rcon, body)
            run_water_damage_budget_auto_preempt(rcon, body)
            run_water_reflex_honest_failure(rcon, body)
            run_lava_reflex_honest_failure_releases_owner(rcon, body)
            run_lava_reflex_skips_non_standable_candidate(rcon, body)
        finally:
            command(rcon, "script in minebot run global_reflex_scan = false", delay=0.0)
            command(rcon, "script in minebot run global_water_reflex_air_threshold = 80", delay=0.0)
            command(rcon, "script in minebot run global_water_reflex_damage_budget = null", delay=0.0)
            command(rcon, f"player {BOT} kill", delay=0.0)
            command(rcon, f"player {TARGET} kill", delay=0.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
