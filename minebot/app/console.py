"""Interactive local-server console for a real MineBot agent.

Run this after starting the local Carpet/RCON test server and configuring a real
LLM provider. Type natural-language goals; MineBot injects them as the current
AgentContext goal and lets the model choose tools through openai-agents.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from minebot.app.config import AppConfigError, provider_registry_from_env
from minebot.app.resource_runtime import ResourceRuntimeConfig, build_resource_agent_runtime
from minebot.brain.lifecycle import LifecycleState
from minebot.game import RconClient, Region, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig


DEFAULT_REGION = Region("local-agent-console", (-32, 0, -32), (32, 128, 32))


def command(rcon: RconClient, command_text: str, delay: float = 0.05) -> str:
    out = rcon.command(command_text)
    if delay:
        time.sleep(delay)
    return out


def prepare_local_server(rcon: RconClient, bot_name: str) -> None:
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
    ]:
        command(rcon, cmd)
    body = ScarpetBody(bot_name, rcon)
    spawned = body.spawn((0, 70, 0))
    if not (spawned.ok and spawned.accepted):
        raise RuntimeError(f"failed to spawn bot {bot_name}: {spawned.error or spawned.data}")
    for cmd in [
        f"tp {bot_name} 0 70 0 -90 0",
        f"gamemode survival {bot_name}",
        f"effect clear {bot_name}",
        "script in minebot run minebot_reset()",
    ]:
        command(rcon, cmd)


async def run_goal(body: ScarpetBody, goal_text: str, *, max_turns: int) -> None:
    provider = provider_registry_from_env()
    parts = build_resource_agent_runtime(
        body=body,
        goal_text=goal_text,
        model_provider=provider,
        config=ResourceRuntimeConfig(natural_region=DEFAULT_REGION),
        agent_name="MineBotConsole",
    )
    try:
        for index in range(max_turns):
            outcome = await parts.runtime.run_turn()
            profile = outcome.profile
            print(
                f"[turn {index + 1}] status={outcome.status} "
                f"lifecycle={outcome.lifecycle.value} situational={profile.situational}"
            )
            _print_recent_tool_results(parts.runtime.trace.snapshot())
            if outcome.status == "yielded":
                print(outcome.message or "yielded")
                return
            if outcome.lifecycle is not LifecycleState.ACTIVE:
                print(f"stopped: lifecycle={outcome.lifecycle.value} message={outcome.message}")
                return
            if _last_collect_succeeded(parts.runtime.trace.snapshot()):
                print("completed: collect_resource reported success")
                return
        print(f"stopped after max_turns={max_turns}; goal may still be in progress")
    finally:
        await provider.aclose()


def _print_recent_tool_results(trace: list[dict[str, object]]) -> None:
    for event in trace[-12:]:
        if event.get("event") == "tool_result":
            print(
                f"  tool={event.get('tool')} success={event.get('success')} "
                f"reason={event.get('reason')}"
            )


def _last_collect_succeeded(trace: list[dict[str, object]]) -> bool:
    return any(
        event.get("event") == "tool_result"
        and event.get("tool") == "collect_resource"
        and event.get("success") is True
        for event in trace
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an interactive MineBot agent against the local test server.")
    parser.add_argument("--bot", default="MineBotLocal")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--once", help="Run one natural-language goal and exit.")
    args = parser.parse_args(argv)

    try:
        provider = provider_registry_from_env()
        provider.resolve("primary")
    except AppConfigError as exc:
        print(f"Provider not configured: {exc}", file=sys.stderr)
        return 2
    else:
        asyncio.run(provider.aclose())

    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        print(f"RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    with rcon:
        prepare_local_server(rcon, args.bot)
        body = ScarpetBody(args.bot, rcon)
        if args.once:
            asyncio.run(run_goal(body, args.once, max_turns=args.max_turns))
            return 0

        print("MineBot console ready. Type a goal, or /quit.")
        while True:
            try:
                goal = input("minebot> ").strip()
            except EOFError:
                print()
                return 0
            if not goal:
                continue
            if goal in {"/quit", "/exit"}:
                return 0
            asyncio.run(run_goal(body, goal, max_turns=args.max_turns))


if __name__ == "__main__":
    raise SystemExit(main())
