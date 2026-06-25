#!/usr/bin/env python3
"""rangedAttack narrow e2e against the local Carpet test server."""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.contract import Action
from minebot.game import ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "E2ERangedBot"
PLAYER_TARGET = "E2ERangedTarget"
BOT_POS = (200, 59, 0)
TARGET_POS = (200, 59, 8)
ANGLE_MATRIX_TAG = "minebot_ranged_angle_target"
RANGED_TARGET_TAGS = [
    "minebot_ranged_target",
    ANGLE_MATRIX_TAG,
    "minebot_ranged_decoy",
    "minebot_ranged_precise_target",
    "minebot_crossbow_target",
    "minebot_ranged_miss_target",
    "minebot_ranged_unknown_target",
    "minebot_ranged_crystal_target",
]


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
        f"player {PLAYER_TARGET} kill",
        "script in minebot run minebot_reset()",
        "script in minebot run global_reflex_scan = false",
        "fill 190 55 -12 215 70 18 air",
        "fill 190 58 -12 215 58 18 stone",
    ]:
        command(rcon, cmd)


def clear_ranged_targets(rcon) -> None:
    for tag in RANGED_TARGET_TAGS:
        command(rcon, f"kill @e[tag={tag}]", delay=0.0)


def read_target_health(rcon, selector: str) -> float | None:
    raw = command(rcon, f"data get entity {selector} Health", delay=0.0)
    tail = raw.rsplit(":", 1)[-1].strip()
    try:
        return float(tail.rstrip("f"))
    except ValueError:
        return None


def read_target_uuid(rcon, selector: str) -> str:
    raw = command(rcon, f"script in minebot run query(entity_selector('{selector}'):0, 'uuid')", delay=0.0)
    value = raw.strip()
    if "=" in value:
        value = value.split("=", 1)[1].strip()
    if " " in value:
        value = value.split(" ", 1)[0].strip()
    if not value or "No entity was found" in value or "Cannot" in value:
        raise AssertionError(f"could not read target uuid for {selector}: {raw!r}")
    return value


def entity_exists(rcon, selector: str) -> bool:
    raw = command(rcon, f"execute if entity {selector} run say exists", delay=0.0)
    return "exists" in raw


def crystal_exists(rcon, selector: str = "@e[type=end_crystal,limit=1]") -> bool:
    raw = command(rcon, f"data get entity {selector} Pos", delay=0.0)
    return "No entity was found" not in raw


def read_arrow_motion_magnitude(rcon, selector: str = "@e[type=arrow,sort=nearest,limit=1]") -> float | None:
    raw = command(rcon, f"data get entity {selector} Motion", delay=0.0)
    if "No entity was found" in raw:
        return None
    tail = raw.rsplit(":", 1)[-1].strip().strip("[]")
    try:
        values = [float(part.strip().rstrip("d")) for part in tail.split(",")]
    except ValueError:
        return None
    if len(values) != 3:
        return None
    return math.sqrt(sum(value * value for value in values))


def wait_for_arrow_motion(rcon, *, timeout_s: float = 3.0) -> float:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        magnitude = read_arrow_motion_magnitude(rcon)
        if magnitude is not None:
            return magnitude
        time.sleep(0.05)
    raise AssertionError("timed out waiting for a launched arrow motion sample")



def entity_count(rcon, selector: str) -> int:
    raw = command(rcon, f"execute store result score #minebot_count minebot run execute if entity {selector}")
    if "commands.generic.num.invalid" in raw:
        raise AssertionError(f"entity count probe failed for {selector}: {raw}")
    score = command(rcon, "scoreboard objectives add minebot dummy", delay=0.0)
    _ = score
    raw = command(rcon, "scoreboard players get #minebot_count minebot", delay=0.0)
    try:
        return int(raw.rsplit(" ", 1)[-1])
    except ValueError as exc:
        raise AssertionError(f"could not parse entity count from {raw!r}") from exc


def prepare_ranged_bot(rcon) -> None:
    clear_ranged_targets(rcon)
    command(rcon, "kill @e[type=arrow]", delay=0.0)
    command(rcon, f"tp {BOT} {BOT_POS[0]} {BOT_POS[1]} {BOT_POS[2]} 0 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"clear {BOT}")
    command(rcon, f"item replace entity {BOT} weapon.offhand with arrow 16")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"player {BOT} stop")


def run_bow_damage_happy_path(rcon, body: ScarpetBody) -> None:
    prepare_ranged_bot(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {TARGET_POS[0]} {TARGET_POS[1]} {TARGET_POS[2]} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_ranged_target"]}',
    )
    before = read_target_health(rcon, "@e[tag=minebot_ranged_target,limit=1]")
    if before is None:
        raise AssertionError("could not read husk health before ranged attack")

    action = Action.create(
        "rangedAttack",
        {
            "weapon": "bow",
            "target_type": "minecraft:husk",
            "radius": 16,
            "timeout_ticks": 120,
            "use_interval_ticks": 22,
            "expected_shots": 1,
        },
    )
    result = body.execute(action)
    if not result.ok or not result.accepted:
        raise AssertionError(f"rangedAttack was not accepted: {result}")
    arrow_motion = wait_for_arrow_motion(rcon)
    event = body.await_action_terminal(action.id, timeout_s=12.0)
    if event.name != "rangedDone":
        raise AssertionError(f"wrong terminal event: {event}")
    if event.data.get("stopped_reason") != "completed":
        raise AssertionError(f"rangedAttack did not complete truthfully: {event.data}")
    if not event.data.get("success"):
        raise AssertionError(f"rangedAttack reported unsuccessful completed result: {event.data}")
    if not event.data.get("damage_observed"):
        raise AssertionError(f"rangedAttack did not report damage truth: {event.data}")
    if not event.data.get("fired_observed"):
        raise AssertionError(f"rangedAttack did not report fired truth: {event.data}")
    if event.data.get("weapon") != "bow":
        raise AssertionError(f"rangedAttack lost weapon truth: {event.data}")
    if not event.data.get("target_id"):
        raise AssertionError(f"rangedAttack did not report target uuid: {event.data}")
    if int(event.data.get("use_interval_ticks") or 0) != 22:
        raise AssertionError(f"rangedAttack interval diagnostics regressed: {event.data}")
    if not (2.8 <= arrow_motion <= 3.2):
        raise AssertionError(
            f"bow rangedAttack did not launch a full-charge arrow: motion={arrow_motion} event={event.data}"
        )

    after = read_target_health(rcon, "@e[tag=minebot_ranged_target,limit=1]")
    if after is None or after >= before:
        raise AssertionError(f"rangedAttack did not lower target health: before={before} after={after} event={event.data}")


def run_bow_angle_matrix_happy_path(rcon, body: ScarpetBody) -> None:
    cases = [
        ("flat", (200, 59, 0), (200, 59, 8)),
        ("yaw_left", (200, 59, 0), (196, 59, 8)),
        ("yaw_right", (200, 59, 0), (204, 59, 8)),
        ("up", (200, 59, 0), (200, 62, 8)),
        ("down", (206, 63, 0), (206, 59, 8)),
    ]
    for label, bot_pos, target_pos in cases:
        prepare_ranged_bot(rcon)
        command(rcon, f"tp {BOT} {bot_pos[0]} {bot_pos[1]} {bot_pos[2]} 0 0")
        if label == "down":
            command(rcon, f"setblock {bot_pos[0]} {bot_pos[1] - 1} {bot_pos[2]} stone")
        command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
        command(rcon, f"player {BOT} hotbar 1")
        command(
            rcon,
            f'summon husk {target_pos[0]} {target_pos[1]} {target_pos[2]} '
            f'{{NoAI:1b,NoGravity:1b,Health:20f,PersistenceRequired:1b,Tags:["{ANGLE_MATRIX_TAG}"]}}',
        )
        before = read_target_health(rcon, f"@e[tag={ANGLE_MATRIX_TAG},limit=1]")
        if before is None:
            raise AssertionError(f"could not read angle-matrix target health before {label} shot")

        event = body.ranged_attack(
            weapon="bow",
            target_type="minecraft:husk",
            radius=24,
            timeout_ticks=120,
            use_interval_ticks=22,
            expected_shots=1,
            timeout_s=12.0,
        )
        if event.name != "rangedDone":
            raise AssertionError(f"wrong terminal event for angle-matrix {label}: {event}")
        if event.data.get("stopped_reason") != "completed":
            raise AssertionError(f"angle-matrix {label} did not complete truthfully: {event.data}")
        if not event.data.get("success"):
            raise AssertionError(f"angle-matrix {label} reported unsuccessful completed result: {event.data}")
        if not event.data.get("damage_observed"):
            raise AssertionError(f"angle-matrix {label} did not report damage truth: {event.data}")
        if not event.data.get("fired_observed"):
            raise AssertionError(f"angle-matrix {label} did not report fired truth: {event.data}")

        after = read_target_health(rcon, f"@e[tag={ANGLE_MATRIX_TAG},limit=1]")
        if after is None or after >= before:
            raise AssertionError(
                f"angle-matrix {label} did not lower target health: before={before} after={after} event={event.data}"
            )
        clear_ranged_targets(rcon)


def run_bow_target_id_happy_path(rcon, body: ScarpetBody) -> None:
    prepare_ranged_bot(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {BOT_POS[0] + 3} {BOT_POS[1]} {BOT_POS[2] + 6} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_ranged_decoy"]}',
    )
    command(
        rcon,
        f'summon husk {BOT_POS[0]} {BOT_POS[1]} {BOT_POS[2] + 9} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_ranged_precise_target"]}',
    )
    decoy_before = read_target_health(rcon, "@e[tag=minebot_ranged_decoy,limit=1]")
    target_before = read_target_health(rcon, "@e[tag=minebot_ranged_precise_target,limit=1]")
    if decoy_before is None or target_before is None:
        raise AssertionError("could not read target-id fixture health")
    target_id = read_target_uuid(rcon, "@e[tag=minebot_ranged_precise_target,limit=1]")

    event = body.ranged_attack(
        weapon="bow",
        target_type="minecraft:husk",
        target_id=target_id,
        radius=16,
        timeout_ticks=120,
        use_interval_ticks=22,
        expected_shots=1,
        timeout_s=12.0,
    )
    if event.name != "rangedDone":
        raise AssertionError(f"wrong terminal event for target-id path: {event}")
    if event.data.get("stopped_reason") != "completed":
        raise AssertionError(f"target-id rangedAttack did not complete truthfully: {event.data}")
    if event.data.get("target_id") != target_id:
        raise AssertionError(f"target-id rangedAttack hit the wrong target: expected={target_id} event={event.data}")
    if not event.data.get("damage_observed"):
        raise AssertionError(f"target-id rangedAttack did not report damage truth: {event.data}")

    decoy_after = read_target_health(rcon, "@e[tag=minebot_ranged_decoy,limit=1]")
    target_after = read_target_health(rcon, "@e[tag=minebot_ranged_precise_target,limit=1]")
    if decoy_after != decoy_before:
        raise AssertionError(
            f"target-id rangedAttack damaged the nearer decoy: before={decoy_before} after={decoy_after} event={event.data}"
        )
    if target_after is None or target_after >= target_before:
        raise AssertionError(
            f"target-id rangedAttack did not damage the selected target: before={target_before} after={target_after} event={event.data}"
        )


def run_crossbow_damage_happy_path(rcon, body: ScarpetBody) -> None:
    prepare_ranged_bot(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with crossbow")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon cow {TARGET_POS[0]} {TARGET_POS[1]} {TARGET_POS[2] - 4} '
        '{NoAI:1b,Health:10f,PersistenceRequired:1b,Tags:["minebot_crossbow_target"]}',
    )
    before = read_target_health(rcon, "@e[tag=minebot_crossbow_target,limit=1]")
    if before is None:
        raise AssertionError("could not read cow health before ranged crossbow attack")

    event = body.ranged_attack(
        weapon="crossbow",
        target_type="minecraft:cow",
        radius=16,
        timeout_ticks=120,
        use_interval_ticks=26,
        expected_shots=1,
        timeout_s=12.0,
    )
    if event.name != "rangedDone":
        raise AssertionError(f"wrong terminal event for crossbow path: {event}")
    if event.data.get("stopped_reason") != "completed":
        raise AssertionError(f"crossbow rangedAttack did not complete truthfully: {event.data}")
    if not event.data.get("success"):
        raise AssertionError(f"crossbow rangedAttack reported unsuccessful completed result: {event.data}")
    if not event.data.get("damage_observed"):
        raise AssertionError(f"crossbow rangedAttack did not report damage truth: {event.data}")
    if not event.data.get("fired_observed"):
        raise AssertionError(f"crossbow rangedAttack did not report fired truth: {event.data}")
    if event.data.get("weapon") != "crossbow":
        raise AssertionError(f"crossbow rangedAttack lost weapon truth: {event.data}")
    if not event.data.get("target_id"):
        raise AssertionError(f"crossbow rangedAttack did not report target uuid: {event.data}")
    if int(event.data.get("use_interval_ticks") or 0) != 26:
        raise AssertionError(f"crossbow rangedAttack interval diagnostics regressed: {event.data}")

    after = read_target_health(rcon, "@e[tag=minebot_crossbow_target,limit=1]")
    if after is None or after >= before:
        raise AssertionError(
            f"crossbow rangedAttack did not lower target health: before={before} after={after} event={event.data}"
        )


def run_player_policy_inverse(rcon, body: ScarpetBody) -> None:
    prepare_ranged_bot(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"player {BOT} hotbar 1")
    target_body = ScarpetBody(PLAYER_TARGET, body.transport)
    spawn_or_fail(target_body, (BOT_POS[0], BOT_POS[1], BOT_POS[2] + 6))
    command(rcon, f"gamemode survival {PLAYER_TARGET}")
    command(rcon, f"tp {PLAYER_TARGET} {BOT_POS[0]} {BOT_POS[1]} {BOT_POS[2] + 6} 180 0")
    command(rcon, f"effect clear {PLAYER_TARGET}")
    health_before = read_target_health(rcon, PLAYER_TARGET)
    arrows_before = entity_exists(rcon, "@e[type=arrow,distance=..20]")

    event = body.ranged_attack(
        weapon="bow",
        target_type="minecraft:player",
        radius=16,
        timeout_ticks=60,
        use_interval_ticks=22,
        expected_shots=1,
        timeout_s=8.0,
    )
    if event.name != "rangedDone":
        raise AssertionError(f"wrong terminal event for player inverse: {event}")
    if event.data.get("stopped_reason") != "player_target_requires_name":
        raise AssertionError(f"rangedAttack did not enforce named-player policy: {event.data}")
    if event.data.get("damage_observed"):
        raise AssertionError(f"named-player policy inverse should not observe damage: {event.data}")
    if event.data.get("fired_observed"):
        raise AssertionError(f"named-player policy inverse should not fire: {event.data}")
    if event.data.get("success"):
        raise AssertionError(f"named-player policy inverse should not succeed: {event.data}")

    health_after = read_target_health(rcon, PLAYER_TARGET)
    arrows_after = entity_exists(rcon, "@e[type=arrow,distance=..20]")
    if health_before is None or health_after is None or health_after != health_before:
        raise AssertionError(
            f"named-player policy inverse should not damage target: before={health_before} after={health_after}"
        )
    if arrows_before != arrows_after:
        raise AssertionError("named-player policy inverse should not fire an arrow")


def run_bow_missed_inverse(rcon, body: ScarpetBody) -> None:
    prepare_ranged_bot(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon armor_stand {TARGET_POS[0] + 10} {TARGET_POS[1]} {TARGET_POS[2] + 10} '
        '{Invisible:1b,NoGravity:1b,Marker:1b,PersistenceRequired:1b,CustomName:\'{"text":"FarMiss"}\',Tags:["minebot_ranged_miss_target"]}',
    )

    event = body.ranged_attack(
        weapon="bow",
        target_type="minecraft:armor_stand",
        radius=32,
        timeout_ticks=80,
        use_interval_ticks=22,
        expected_shots=1,
        timeout_s=10.0,
    )
    if event.name != "rangedDone":
        raise AssertionError(f"wrong terminal event for missed inverse: {event}")
    if event.data.get("stopped_reason") != "missed":
        raise AssertionError(f"rangedAttack did not classify fired no-damage miss truthfully: {event.data}")
    if event.data.get("success"):
        raise AssertionError(f"missed inverse should not succeed: {event.data}")
    if event.data.get("damage_observed"):
        raise AssertionError(f"missed inverse should not observe damage: {event.data}")
    if not event.data.get("fired_observed"):
        raise AssertionError(f"missed inverse should still observe a fired shot: {event.data}")


def run_bow_unknown_inverse(rcon, body: ScarpetBody) -> None:
    prepare_ranged_bot(rcon)
    command(rcon, f"item replace entity {BOT} hotbar.0 with bow")
    command(rcon, f"item replace entity {BOT} weapon.offhand with air")
    command(rcon, f"player {BOT} hotbar 1")
    command(
        rcon,
        f'summon husk {TARGET_POS[0]} {TARGET_POS[1]} {TARGET_POS[2]} '
        '{NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_ranged_unknown_target"]}',
    )

    event = body.ranged_attack(
        weapon="bow",
        target_type="minecraft:husk",
        radius=16,
        timeout_ticks=90,
        use_interval_ticks=22,
        expected_shots=1,
        timeout_s=12.0,
    )
    if event.name != "rangedDone":
        raise AssertionError(f"wrong terminal event for unknown inverse: {event}")
    if event.data.get("stopped_reason") != "unknown":
        raise AssertionError(f"rangedAttack did not classify no-fire timeout truthfully: {event.data}")
    if event.data.get("success"):
        raise AssertionError(f"unknown inverse should not succeed: {event.data}")
    if event.data.get("damage_observed"):
        raise AssertionError(f"unknown inverse should not observe damage: {event.data}")
    if event.data.get("fired_observed"):
        raise AssertionError(f"unknown inverse should not claim a fired shot: {event.data}")


def run_case(case_fn) -> None:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        try:
            spawn_or_fail(body, (0, 59, 0))
            command(rcon, f"tp {BOT} {BOT_POS[0]} {BOT_POS[1]} {BOT_POS[2]} 0 0")
            case_fn(rcon, body)
        finally:
            command(rcon, f"player {BOT} kill", delay=0.0)
            command(rcon, f"player {PLAYER_TARGET} kill", delay=0.0)


def main() -> int:
    for case_fn in [
        run_bow_damage_happy_path,
        run_bow_angle_matrix_happy_path,
        run_bow_target_id_happy_path,
        run_crossbow_damage_happy_path,
        run_player_policy_inverse,
        run_bow_missed_inverse,
        run_bow_unknown_inverse,
    ]:
        run_case(case_fn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
