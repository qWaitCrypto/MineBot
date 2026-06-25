#!/usr/bin/env python3
"""attackEntity e2e against the local Carpet test server."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig
from tests.e2e_support import spawn_or_fail


BOT = "E2EAttackBot"
PLAYER_TARGET = "E2EAttackTarget"
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
        "gamerule doMobSpawning false",
        "time set day",
        "kill @e[type=!player]",
        f"player {BOT} kill",
        f"player {PLAYER_TARGET} kill",
    ]:
        command(rcon, cmd)


def run_hostile_happy_path(rcon: RconClient, body: ScarpetBody) -> None:
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with diamond_sword")
    command(rcon, f"player {BOT} hotbar 1")
    command(rcon, 'summon husk 2 59 0 {NoAI:1b,Health:20f,PersistenceRequired:1b,Tags:["minebot_attack_target"]}')

    event = body.attack_entity(
        target_type="minecraft:husk",
        radius=5,
        timeout_ticks=160,
        cooldown_ticks=8,
        timeout_s=12.0,
    )
    if event.name != "attackDone":
        raise AssertionError(f"wrong terminal event: {event}")
    if event.data.get("stopped_reason") != "killed":
        raise AssertionError(f"attackEntity did not classify persistent mob kill truthfully: {event.data}")
    if int(event.data.get("attacks") or 0) <= 0:
        raise AssertionError(f"attackEntity did not issue attacks: {event.data}")
    if not event.data.get("damage_observed"):
        raise AssertionError(f"attackEntity did not report damage truth: {event.data}")
    if not event.data.get("persistent_target"):
        raise AssertionError(f"attackEntity lost persistent-target truth: {event.data}")
    if not event.data.get("target_id"):
        raise AssertionError(f"attackEntity did not report stable target uuid: {event.data}")
    min_interval = event.data.get("min_attack_interval_ticks")
    if min_interval is None or int(min_interval) < 8:
        raise AssertionError(f"attackEntity cooldown diagnostics regressed: {event.data}")
    remaining = command(rcon, 'execute if entity @e[type=husk,tag=minebot_attack_target] run say alive', delay=0.0)
    if "alive" in remaining:
        raise AssertionError("hostile target still existed after killed result")


def run_player_policy_inverse(rcon: RconClient, body: ScarpetBody) -> None:
    spawn_or_fail(ScarpetBody(PLAYER_TARGET, rcon), (2, 59, 0))
    command(rcon, f"gamemode survival {PLAYER_TARGET}")
    command(rcon, f"tp {PLAYER_TARGET} 2 59 0 90 0")
    event = body.attack_entity(
        target_type="minecraft:player",
        radius=5,
        timeout_ticks=40,
        cooldown_ticks=8,
        timeout_s=6.0,
    )
    if event.data.get("stopped_reason") != "player_target_requires_name":
        raise AssertionError(f"attackEntity did not enforce player targeting policy: {event.data}")
    if int(event.data.get("attacks") or 0) != 0:
        raise AssertionError(f"player policy inverse should not attack: {event.data}")


def run_named_player_damage_path(rcon: RconClient, body: ScarpetBody) -> None:
    command(rcon, f"tp {BOT} 0 59 0 -90 0")
    command(rcon, f"item replace entity {BOT} hotbar.0 with diamond_sword")
    command(rcon, f"player {BOT} hotbar 1")
    command(rcon, f"item replace entity {PLAYER_TARGET} hotbar.0 with air")
    command(rcon, f"effect clear {PLAYER_TARGET}")
    command(rcon, f"attribute {PLAYER_TARGET} minecraft:generic.max_health base set 20")
    command(rcon, f"tp {PLAYER_TARGET} 2 59 0 90 0")
    command(rcon, f"damage {PLAYER_TARGET} 16 generic", delay=0.2)

    event = body.attack_entity(
        target_type="minecraft:player",
        target_name=PLAYER_TARGET,
        radius=5,
        timeout_ticks=80,
        cooldown_ticks=8,
        timeout_s=8.0,
    )
    if event.name != "attackDone":
        raise AssertionError(f"wrong terminal event for named player target: {event}")
    if event.data.get("target_name") != PLAYER_TARGET:
        raise AssertionError(f"attackEntity did not preserve named player target identity: {event.data}")
    if not event.data.get("target_id"):
        raise AssertionError(f"attackEntity did not report player target uuid: {event.data}")
    if int(event.data.get("attacks") or 0) <= 0:
        raise AssertionError(f"named player target path issued no attacks: {event.data}")
    if event.data.get("stopped_reason") not in {"killed", "target_gone", "timeout"}:
        raise AssertionError(f"named player target path returned wrong terminal reason: {event.data}")
    if not event.data.get("damage_observed"):
        raise AssertionError(f"named player target path did not observe damage: {event.data}")
    min_interval = event.data.get("min_attack_interval_ticks")
    if min_interval is None or int(min_interval) < 8:
        raise AssertionError(f"named player target cooldown diagnostics regressed: {event.data}")


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
        spawn_or_fail(body, (0, 59, 0))
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, "script in minebot run minebot_reset()")
        run_hostile_happy_path(rcon, body)
        run_player_policy_inverse(rcon, body)
        run_named_player_damage_path(rcon, body)


if __name__ == "__main__":
    main()
