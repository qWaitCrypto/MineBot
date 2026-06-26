#!/usr/bin/env python3
"""Real-model e2e for Agent Phase-1 collect_resource.

This is the long-test entry that replaces deterministic smoke tests once an API
key is available. It runs openai-agents Runner.run with a real ModelProvider and
expects the model to call the registered collect_resource tool.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.config import AppConfigError, provider_registry_from_env  # noqa: E402
from minebot.app.resource_runtime import ResourceRuntimeConfig, build_resource_agent_runtime, inventory_count  # noqa: E402
from minebot.brain.composition import CompositionBudget  # noqa: E402
from minebot.game import RconClient, Region, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402


BOT = "E2EAgentRealModel"
REGION = Region("agent-real-model", (-10, 0, -10), (16, 100, 10))


def command(rcon: RconClient, command_text: str, delay: float = 0.05) -> str:
    out = rcon.command(command_text)
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
        f"player {BOT} kill",
        "fill -10 70 -10 16 78 10 air",
        "fill -10 69 -10 16 69 10 stone",
    ]:
        command(rcon, cmd)


def reset_subject(rcon: RconClient, *, item: str = "dirt", count: int = 4) -> None:
    for cmd in [
        "script in minebot run minebot_reset()",
        "kill @e[type=!player]",
        "fill -10 70 -10 16 78 10 air",
        "fill -10 69 -10 16 69 10 stone",
        f"clear {BOT}",
        f"tp {BOT} 0 70 0 -90 0",
        f"gamemode survival {BOT}",
        f"effect clear {BOT}",
    ]:
        command(rcon, cmd)
    for offset in range(count):
        command(rcon, f"setblock {3 + offset} 70 0 {item}", delay=0.0)


async def run_real_model_collect(body: ScarpetBody) -> dict[str, object]:
    model_provider = provider_registry_from_env()
    parts = build_resource_agent_runtime(
        body=body,
        goal_text="Collect exactly 3 dirt blocks using the collect_resource tool. Stop after the tool reports success.",
        model_provider=model_provider,
        config=ResourceRuntimeConfig(
            natural_region=REGION,
            budget=CompositionBudget(max_candidates=6, max_mutating_calls=6, max_wall_s=60.0),
        ),
        agent_name="MineBotRealModelE2E",
    )
    outcome = await parts.runtime.run_turn()
    after = inventory_count(body, "dirt")
    trace = parts.runtime.trace.snapshot()
    collect_results = [
        event for event in trace
        if event.get("event") == "tool_result" and event.get("tool") == "collect_resource"
    ]
    if outcome.status not in {"completed_turn", "yielded"}:
        raise AssertionError(f"unexpected model turn status: {outcome}")
    if after < 3:
        raise AssertionError(f"model did not complete collect_resource: after={after} outcome={outcome} trace={trace}")
    if not any(event.get("success") is True for event in collect_results):
        raise AssertionError(f"model did not call collect_resource successfully: trace={trace}")
    await model_provider.aclose()
    return {
        "status": outcome.status,
        "after_count": after,
        "tool_results": collect_results,
        "trace_events": [event.get("event") for event in trace],
    }


def main() -> None:
    try:
        provider = provider_registry_from_env()
        provider.resolve("primary")
    except AppConfigError as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: real model provider not configured: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)
    else:
        asyncio.run(provider.aclose())

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
        spawn_or_fail(body, (0, 70, 0))
        reset_subject(rcon, item="dirt", count=4)
        result = asyncio.run(run_real_model_collect(body))
        print(result)


if __name__ == "__main__":
    main()
