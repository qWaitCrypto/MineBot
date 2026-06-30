#!/usr/bin/env python3
"""Physical dig_down_to_y e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.governance import BreakContext, GovernancePolicy, Region
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EDigDownBot"
ORIGIN = (130, 64, 0)
TARGET_Y = 62
SKIP_EXIT_CODE = 77


def command(rcon: RconClient, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def setup_world(rcon: RconClient) -> None:
    for cmd in [
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
        "fill 126 61 -4 134 70 4 air",
        "fill 126 60 -4 134 60 4 stone",
        "fill 126 61 -4 134 63 4 stone",
    ]:
        command(rcon, cmd)


def reset_happy_world(rcon: RconClient) -> None:
    setup_world(rcon)
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    command(rcon, "script in minebot run minebot_reset()")


def reset_fall_risk_world(rcon: RconClient) -> None:
    setup_world(rcon)
    command(rcon, "fill 126 60 -4 134 63 4 air")
    command(rcon, "fill 126 59 -4 134 59 4 stone")
    command(rcon, f"setblock {ORIGIN[0]} 63 {ORIGIN[2]} stone")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    command(rcon, "script in minebot run minebot_reset()")


def reset_target_liquid_world(rcon: RconClient) -> None:
    setup_world(rcon)
    command(rcon, f"setblock {ORIGIN[0]} 63 {ORIGIN[2]} water")
    command(rcon, f"setblock {ORIGIN[0]} 62 {ORIGIN[2]} stone")
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    command(rcon, "script in minebot run minebot_reset()")


def make_runtime(body: ScarpetBody) -> BlockWork:
    policy = GovernancePolicy(natural_regions=[Region("e2e-dig-down", (126, 55, -4), (134, 80, 4))])
    return BlockWork(body, policy)


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_happy_world(rcon)
    runtime = make_runtime(body)
    result = runtime.dig_down_to_y(
        TARGET_Y,
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        max_clear_fall=2,
        dig_timeout_s=20.0,
        move_timeout_s=10.0,
    )
    final = body.get_state()
    opened_63 = body.perceive("blockAt", {"x": ORIGIN[0], "y": 63, "z": ORIGIN[2]})
    opened_62 = body.perceive("blockAt", {"x": ORIGIN[0], "y": 62, "z": ORIGIN[2]})

    if not result.success:
        raise AssertionError(f"dig_down_to_y failed: {result}")
    if result.reason != "dig_down_target_reached":
        raise AssertionError(f"unexpected dig_down_to_y reason: {result.reason}")
    if int(final.pos[1]) > TARGET_Y:
        raise AssertionError(f"final Y did not reach target: final={final.pos} target_y={TARGET_Y}")
    if opened_63.data.get("state") != "CLEAR":
        raise AssertionError(f"first opened block is not clear: {opened_63}")
    if opened_62.data.get("state") != "CLEAR":
        raise AssertionError(f"second opened block is not clear: {opened_62}")
    if result.metrics.get("steps_completed") != 2:
        raise AssertionError(f"expected 2 completed descent steps, got: {result.metrics}")

    return {
        "origin": ORIGIN,
        "target_y": TARGET_Y,
        "final": final.pos,
        "steps_completed": result.metrics.get("steps_completed"),
        "step_reasons": [step.get("reason") for step in result.metrics.get("steps", [])],
    }


def run_fall_risk_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_fall_risk_world(rcon)
    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.dig_down_to_y(
        TARGET_Y,
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        max_clear_fall=2,
        dig_timeout_s=8.0,
        move_timeout_s=4.0,
    )
    after = body.get_state()
    floor = body.perceive("blockAt", {"x": ORIGIN[0], "y": 63, "z": ORIGIN[2]})

    if result.success or result.reason != "dig_down_fall_risk":
        raise AssertionError(f"dig_down fall-risk inverse returned wrong truth: {result.to_payload()}")
    if before.pos != after.pos:
        raise AssertionError(f"dig_down fall-risk inverse moved the body: before={before.pos} after={after.pos}")
    if floor.data.get("state") != "SOLID":
        raise AssertionError(f"dig_down fall-risk inverse mined floor: floor={floor.data} result={result.to_payload()}")
    return {
        "reason": result.reason,
        "can_retry": result.can_retry,
        "before": before.pos,
        "after": after.pos,
        "fall_clearance": result.metrics.get("steps", [{}])[-1].get("metrics", {}).get("fall_clearance"),
    }


def run_target_liquid_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_target_liquid_world(rcon)
    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.dig_down_to_y(
        TARGET_Y,
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        max_clear_fall=2,
        dig_timeout_s=8.0,
        move_timeout_s=4.0,
    )
    after = body.get_state()
    floor = body.perceive("blockAt", {"x": ORIGIN[0], "y": 63, "z": ORIGIN[2]})

    if result.success or result.reason != "dig_down_target_liquid":
        raise AssertionError(f"dig_down target-liquid inverse returned wrong truth: {result.to_payload()}")
    if abs(after.pos[0] - before.pos[0]) > 0.1 or abs(after.pos[2] - before.pos[2]) > 0.1:
        raise AssertionError(f"dig_down target-liquid inverse drifted sideways: before={before.pos} after={after.pos}")
    if abs(after.pos[1] - before.pos[1]) > 0.5:
        raise AssertionError(f"dig_down target-liquid inverse drifted vertically too far: before={before.pos} after={after.pos}")
    if floor.data.get("type") not in {"water", "minecraft:water"}:
        raise AssertionError(f"dig_down target-liquid inverse mutated liquid floor: {floor.data} result={result.to_payload()}")
    return {
        "reason": result.reason,
        "can_retry": result.can_retry,
        "target": result.metrics.get("target"),
        "before": before.pos,
        "after": after.pos,
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
        spawn_or_fail(body, ORIGIN)

        cases = {
            "happy": lambda: run_happy_path(rcon, body),
            "fall_risk": lambda: run_fall_risk_inverse(rcon, body),
            "target_liquid": lambda: run_target_liquid_inverse(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_DIG_DOWN_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown dig-down e2e cases: {unknown}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
