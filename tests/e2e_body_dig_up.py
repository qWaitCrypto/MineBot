#!/usr/bin/env python3
"""Physical dig_up_to_y e2e against the local Carpet test server."""

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


BOT = "E2EDigUpBot"
ORIGIN = (150, 64, 0)
TARGET_Y = 66
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
        "fill 146 63 -4 154 71 4 air",
        "fill 146 63 -4 154 63 4 stone",
        "setblock 150 65 0 dirt",
        "setblock 150 66 0 dirt",
        "setblock 150 67 0 dirt",
    ]:
        command(rcon, cmd)


def reset_happy_world(rcon: RconClient) -> None:
    setup_world(rcon)
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")
    command(rcon, f"item replace entity {BOT} hotbar.1 with dirt 16")
    command(rcon, "script in minebot run minebot_reset()")


def reset_no_scaffold_world(rcon: RconClient) -> None:
    setup_world(rcon)
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"clear {BOT}")
    command(rcon, "script in minebot run minebot_reset()")


def reset_liquid_above_world(rcon: RconClient) -> None:
    setup_world(rcon)
    command(rcon, f"tp {BOT} {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} 0 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"item replace entity {BOT} hotbar.0 with cobblestone 16")
    command(rcon, f"setblock {ORIGIN[0]} 65 {ORIGIN[2]} water")
    command(rcon, f"setblock {ORIGIN[0]} 66 {ORIGIN[2]} air")
    command(rcon, "script in minebot run minebot_reset()")


def make_runtime(body: ScarpetBody) -> BlockWork:
    policy = GovernancePolicy(natural_regions=[Region("e2e-dig-up", (146, 55, -4), (154, 80, 4))])
    return BlockWork(body, policy)


def run_happy_path(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_happy_world(rcon)
    runtime = make_runtime(body)
    result = runtime.dig_up_to_y(
        TARGET_Y,
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=20.0,
    )
    final = body.get_state()
    pillar_64 = body.perceive("blockAt", {"x": ORIGIN[0], "y": 64, "z": ORIGIN[2]})
    pillar_65 = body.perceive("blockAt", {"x": ORIGIN[0], "y": 65, "z": ORIGIN[2]})

    if not result.success:
        raise AssertionError(f"dig_up_to_y failed: {result}")
    if result.reason != "dig_up_target_reached":
        raise AssertionError(f"unexpected dig_up_to_y reason: {result.reason}")
    if int(final.pos[1]) < TARGET_Y:
        raise AssertionError(f"final Y did not reach target: final={final.pos} target_y={TARGET_Y}")
    if pillar_64.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"first pillar block missing: {pillar_64}")
    if pillar_65.data.get("type") not in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"second pillar block missing: {pillar_65}")
    if result.metrics.get("steps_completed") != 2:
        raise AssertionError(f"expected 2 completed ascent steps, got: {result.metrics}")

    return {
        "origin": ORIGIN,
        "target_y": TARGET_Y,
        "final": final.pos,
        "steps_completed": result.metrics.get("steps_completed"),
        "step_reasons": [step.get("reason") for step in result.metrics.get("steps", [])],
        "pillar": [pillar_64.data, pillar_65.data],
    }


def run_no_scaffold_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_no_scaffold_world(rcon)
    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.dig_up_to_y(
        TARGET_Y,
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=8.0,
    )
    after = body.get_state()
    head = body.perceive("blockAt", {"x": ORIGIN[0], "y": 65, "z": ORIGIN[2]})
    pillar = body.perceive("blockAt", {"x": ORIGIN[0], "y": 64, "z": ORIGIN[2]})

    if result.success or result.reason != "dig_up_no_scaffold_available":
        raise AssertionError(f"dig_up no-scaffold inverse returned wrong truth: {result.to_payload()}")
    if before.pos != after.pos:
        raise AssertionError(f"dig_up no-scaffold inverse moved the body: before={before.pos} after={after.pos}")
    if head.data.get("type") not in {"dirt", "minecraft:dirt"}:
        raise AssertionError(f"dig_up no-scaffold inverse mutated head block: {head.data} result={result.to_payload()}")
    if pillar.data.get("type") in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"dig_up no-scaffold inverse placed a pillar: {pillar.data} result={result.to_payload()}")
    return {"reason": result.reason, "can_retry": result.can_retry, "before": before.pos, "after": after.pos}


def run_liquid_above_inverse(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    reset_liquid_above_world(rcon)
    runtime = make_runtime(body)
    before = body.get_state()
    result = runtime.dig_up_to_y(
        TARGET_Y,
        current_pos=ORIGIN,
        context=BreakContext.DIRECT,
        scaffold_blocks=("cobblestone",),
        timeout_s=8.0,
    )
    after = body.get_state()
    head = body.perceive("blockAt", {"x": ORIGIN[0], "y": 65, "z": ORIGIN[2]})
    pillar = body.perceive("blockAt", {"x": ORIGIN[0], "y": 64, "z": ORIGIN[2]})

    if result.success or result.reason != "dig_up_liquid_above":
        raise AssertionError(f"dig_up liquid-above inverse returned wrong truth: {result.to_payload()}")
    if before.pos != after.pos:
        raise AssertionError(f"dig_up liquid-above inverse moved the body: before={before.pos} after={after.pos}")
    if head.data.get("type") not in {"water", "minecraft:water"}:
        raise AssertionError(f"dig_up liquid-above inverse mutated liquid head block: {head.data} result={result.to_payload()}")
    if pillar.data.get("type") in {"cobblestone", "minecraft:cobblestone"}:
        raise AssertionError(f"dig_up liquid-above inverse placed a pillar: {pillar.data} result={result.to_payload()}")
    return {
        "reason": result.reason,
        "can_retry": result.can_retry,
        "phase": result.metrics.get("phase"),
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
            "no_scaffold": lambda: run_no_scaffold_inverse(rcon, body),
            "liquid_above": lambda: run_liquid_above_inverse(rcon, body),
        }
        selected_raw = os.environ.get("MINEBOT_DIG_UP_CASES")
        selected = [name.strip() for name in selected_raw.split(",") if name.strip()] if selected_raw else list(cases.keys())
        unknown = [name for name in selected if name not in cases]
        if unknown:
            raise AssertionError(f"unknown dig-up e2e cases: {unknown}")
        print({name: cases[name]() for name in selected})


if __name__ == "__main__":
    main()
