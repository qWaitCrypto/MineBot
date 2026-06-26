"""Interactive local-server console for a real MineBot agent.

Run this after starting the local Carpet/RCON test server and configuring a real
LLM provider. Type natural-language goals; MineBot injects them as the current
AgentContext goal and lets the model choose tools through openai-agents.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time

from openai import APIStatusError
from minebot.app.config import AppConfigError, agent_language_from_env, provider_registry_from_env
from minebot.app.session import DEFAULT_RUNAWAY_STEP_LIMIT
from minebot.app.resource_runtime import ResourceRuntimeConfig, build_resource_agent_runtime, inventory_count
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


def prepare_local_server(rcon: RconClient, bot_name: str, *, seed_demo_resources: bool) -> None:
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
        f"clear {bot_name}",
        "script in minebot run minebot_reset()",
    ]:
        command(rcon, cmd)
    if seed_demo_resources:
        seed_resource_scene(rcon)


def seed_resource_scene(rcon: RconClient) -> None:
    """Create a small reproducible natural-resource patch for local console e2e."""
    for cmd in [
        "fill -10 70 -10 16 78 10 air",
        "fill -10 69 -10 16 69 10 stone",
    ]:
        command(rcon, cmd)
    for offset in range(4):
        command(rcon, f"setblock {3 + offset} 70 0 dirt", delay=0.0)


async def run_goal(body: ScarpetBody, goal_text: str, *, max_turns: int, sdk_max_turns: int | None, language: str) -> None:
    provider = provider_registry_from_env()
    collect_target = parse_collect_goal(goal_text)
    parts = build_resource_agent_runtime(
        body=body,
        goal_text=goal_text,
        model_provider=provider,
        config=ResourceRuntimeConfig(natural_region=DEFAULT_REGION),
        agent_name="MineBotConsole",
        language=language,
    )
    parts.runtime.max_turns = sdk_max_turns
    try:
        printed_events = 0
        for index in range(max_turns):
            outcome = await parts.runtime.run_turn()
            profile = outcome.profile
            print(
                f"[turn {index + 1}] status={outcome.status} "
                f"lifecycle={outcome.lifecycle.value} situational={profile.situational}"
            )
            trace = parts.runtime.trace.snapshot()
            printed_events = _print_new_observations(trace, printed_events)
            if outcome.status == "yielded":
                print(outcome.message or "yielded")
                return
            if outcome.lifecycle is not LifecycleState.ACTIVE:
                print(f"stopped: lifecycle={outcome.lifecycle.value} message={outcome.message}")
                return
            if _goal_completed(body, trace, collect_target):
                print("completed: authoritative inventory satisfies goal")
                return
        print(f"stopped after runaway guard max_turns={max_turns}; goal may still be in progress")
    finally:
        await provider.aclose()


def _print_new_observations(trace: list[dict[str, object]], start_index: int) -> int:
    for event in trace[start_index:]:
        kind = event.get("event")
        if kind in {"assistant_message", "assistant_final_output"}:
            content = str(event.get("content") or "").strip()
            if content:
                print(f"MineBot: {content}")
        elif kind == "assistant_no_content_tool_only":
            print("MineBot: (tool calls only; no visible assistant message)")
        elif kind == "model_tool_call":
            args = event.get("arguments_summary")
            suffix = f" args={args}" if args else ""
            print(f"  call={event.get('tool')}{suffix}")
        elif kind == "tool_result":
            print(
                f"  tool={event.get('tool')} success={event.get('success')} "
                f"reason={event.get('reason')}"
            )
    return len(trace)


def _last_collect_succeeded(trace: list[dict[str, object]]) -> bool:
    return any(
        event.get("event") == "tool_result"
        and event.get("tool") == "collect_resource"
        and event.get("success") is True
        for event in trace
    )


def _goal_completed(
    body: ScarpetBody,
    trace: list[dict[str, object]],
    collect_target: tuple[str, int] | None,
) -> bool:
    if _last_collect_succeeded(trace):
        return True
    if collect_target is None:
        return False
    item, count = collect_target
    return inventory_count(body, item) >= count


def parse_collect_goal(goal_text: str) -> tuple[str, int] | None:
    text = goal_text.strip().lower().replace("minecraft:", "")
    match = re.search(r"\b(?:collect|get|gather|mine)\s+(\d+)\s+([a-z_]+)\b", text)
    if match:
        return match.group(2), int(match.group(1))
    match = re.search(r"\b(?:collect|get|gather|mine)\s+([a-z_]+)\s+(\d+)\b", text)
    if match:
        return match.group(1), int(match.group(2))
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an interactive MineBot agent against the local test server.")
    parser.add_argument("--bot", default="MineBotLocal")
    parser.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_RUNAWAY_STEP_LIMIT,
        help="Console runaway guard; normal stopping is lifecycle/progress/terminal truth.",
    )
    parser.add_argument(
        "--sdk-max-turns",
        type=int,
        default=None,
        help="Optional SDK runaway guard; omit for progress-authority stopping.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Visible speech language for MineBot. Defaults to MINEBOT_AGENT_LANGUAGE or Chinese in local console.",
    )
    parser.add_argument(
        "--no-demo-resources",
        action="store_true",
        help="Do not seed the small local dirt patch used by collect-resource console tests.",
    )
    parser.add_argument("--once", help="Run one natural-language goal and exit.")
    args = parser.parse_args(argv)
    language = args.language or agent_language_from_env(default="Chinese")

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
        prepare_local_server(rcon, args.bot, seed_demo_resources=not args.no_demo_resources)
        body = ScarpetBody(args.bot, rcon)
        if args.once:
            try:
                asyncio.run(
                    run_goal(
                        body,
                        args.once,
                        max_turns=args.max_turns,
                        sdk_max_turns=args.sdk_max_turns,
                        language=language,
                    )
                )
            except APIStatusError as exc:
                print(f"Model provider error: {type(exc).__name__} status={exc.status_code}", file=sys.stderr)
                return 4
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
            try:
                asyncio.run(
                    run_goal(
                        body,
                        goal,
                        max_turns=args.max_turns,
                        sdk_max_turns=args.sdk_max_turns,
                        language=language,
                    )
                )
            except APIStatusError as exc:
                print(f"Model provider error: {type(exc).__name__} status={exc.status_code}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
