#!/usr/bin/env python3
"""Live proof for the AgentRuntime spine against the local test server."""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.runner import AgentRuntime, RuntimeRunContext  # noqa: E402
from minebot.brain.context import AgentContext  # noqa: E402
from minebot.brain.lifecycle import LifecycleController, LifecycleState  # noqa: E402
from minebot.brain.modes import ModeRuntime  # noqa: E402
from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar  # noqa: E402
from minebot.contract import Action, ToolResult, terminal_event_to_tool_result  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402

BOT = "E2EAgentBot"


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
        "fill -4 59 -4 20 66 8 air",
        "fill -4 58 -4 20 58 8 stone",
    ]:
        command(rcon, cmd)


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def move_tool(body: ScarpetBody) -> RegisteredTool:
    def callable_(params: dict[str, object]) -> ToolResult:
        target = params.get("target")
        if not isinstance(target, list) or len(target) != 3:
            return ToolResult(False, "invalid_target", False)
        timeout_s = float(params.get("timeout_s") or 15.0)
        action = Action.create("moveTo", {"target": target})
        accepted = body.execute(action)
        if not (accepted.ok and accepted.accepted):
            return ToolResult(False, accepted.error or "move_rejected", True, metrics=dict(accepted.data))
        terminal = body.await_action_terminal(action.id, timeout_s=timeout_s)
        return terminal_event_to_tool_result(terminal)

    return RegisteredTool(
        name="move_to",
        description="Move to a target coordinate through the Body layer.",
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
                "timeout_s": {"type": "number"},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        callable=callable_,
        sidecar=ToolSidecar(
            progress_key="move_to",
            mutating=True,
            permission="move",
            body_scope=("movement",),
            terminal_truth=("moveDone",),
            timeout_s=20.0,
        ),
    )


def make_runtime(body: ScarpetBody, runner_run) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(move_tool(body))
    return AgentRuntime(
        body=body,
        registry=registry,
        agent_context=AgentContext(
            system_prompt="You are MineBot. Use tools only when needed.",
            goal_text="Move to the live e2e target coordinate.",
        ),
        lifecycle=LifecycleController(),
        mode_runtime=ModeRuntime(),
        authority=ProgressAuthority(),
        runner_run=runner_run,
        max_turns=2,
    )


async def call_sdk_tool(agent, context, target, timeout_s):
    tool = next(tool for tool in agent.tools if tool.name == "move_to")

    class Wrapper:
        def __init__(self, context):
            self.context = context

    import json

    return await tool.on_invoke_tool(
        Wrapper(context),
        json.dumps({"target": list(target), "timeout_s": timeout_s}),
    )


async def run_projection_refusal(body: ScarpetBody) -> dict[str, object]:
    target = (4, 59, -2)
    runtime = make_runtime(body, lambda *args, **kwargs: None)
    runtime.set_tool_facts("move_to", {"precondition_missing": True})
    profile = runtime.mode_runtime.profile_for(LifecycleState.ACTIVE)
    context = RuntimeRunContext(
        agent_context=runtime.agent_context,
        weld_context=runtime.weld_context,
        profile=profile,
        tool_facts=runtime.tool_facts,
        trace=runtime.trace,
    )
    tool = next(tool for tool in runtime.agent.tools if tool.name == "move_to")

    class Wrapper:
        def __init__(self, context):
            self.context = context

    wrapper = Wrapper(context)
    before = body.get_state()
    if tool.is_enabled(wrapper, runtime.agent):
        raise AssertionError("move_to should be disabled by missing precondition")
    after_disabled = body.get_state()
    if distance(before.pos, after_disabled.pos) > 0.25:
        raise AssertionError(f"disabled tool moved the body: before={before.pos} after={after_disabled.pos}")

    runtime.set_tool_facts("move_to", {})
    context.tool_facts = runtime.tool_facts
    if not tool.is_enabled(wrapper, runtime.agent):
        raise AssertionError("move_to should be enabled after precondition clears")
    result = await call_sdk_tool(runtime.agent, context, target, 12.0)
    final = body.get_state()
    dist = distance(final.pos, target)
    if not result.get("success") or dist > 1.0:
        raise AssertionError(f"enabled move_to did not complete: result={result} final={final.pos} dist={dist:.3f}")
    trace = runtime.trace.snapshot()
    if not any(event.get("event") == "tool_enabled" and event.get("enabled") is False for event in trace):
        raise AssertionError(f"trace missing disabled projection event: {trace}")
    if not any(event.get("event") == "tool_enabled" and event.get("enabled") is True for event in trace):
        raise AssertionError(f"trace missing enabled projection event: {trace}")
    if not any(event.get("event") == "tool_result" and event.get("reason") == "arrived" for event in trace):
        raise AssertionError(f"trace missing tool result event: {trace}")
    return {
        "disabled_static": after_disabled.pos,
        "enabled_final": final.pos,
        "enabled_dist": round(dist, 3),
        "trace_events": [event.get("event") for event in trace],
    }


async def run_happy(body: ScarpetBody) -> dict[str, object]:
    target = (8, 59, 0)

    async def runner(agent, input_text, *, context=None, **kwargs):
        return await call_sdk_tool(agent, context, target, 15.0)

    runtime = make_runtime(body, runner)
    outcome = await runtime.run_turn()
    final = body.get_state()
    dist = distance(final.pos, target)
    if outcome.status != "completed_turn":
        raise AssertionError(f"expected completed turn, got {outcome}")
    if runtime.lifecycle.state is not LifecycleState.ACTIVE:
        raise AssertionError(f"expected ACTIVE, got {runtime.lifecycle.state}")
    if dist > 1.0:
        raise AssertionError(f"final position too far: {final.pos} target={target} dist={dist:.3f}")
    if runtime.authority.should_yield():
        raise AssertionError("authority yielded on happy path")
    return {"target": target, "final": final.pos, "dist": round(dist, 3)}


async def run_yield(body: ScarpetBody) -> dict[str, object]:
    target = (12, 59, 5)

    async def runner(agent, input_text, *, context=None, **kwargs):
        result = None
        for _ in range(8):
            result = await call_sdk_tool(agent, context, target, 4.0)
        return result

    runtime = make_runtime(body, runner)
    outcome = await runtime.run_turn()
    final = body.get_state()
    if outcome.status != "yielded":
        raise AssertionError(f"expected yielded, got {outcome}")
    if runtime.lifecycle.state is not LifecycleState.YIELDED:
        raise AssertionError(f"expected YIELDED, got {runtime.lifecycle.state}")
    if final.pos[0] >= 4.5:
        raise AssertionError(f"blocked bot crossed wall: pos={final.pos}")
    if outcome.yielded_facts is None or not (
        outcome.yielded_facts.stagnant_steps > 0
        or outcome.yielded_facts.stalled_steps > 0
        or outcome.yielded_facts.failure_steps > 0
    ):
        raise AssertionError(f"missing useful progress facts: {outcome}")
    if "How should I continue?" not in (outcome.message or ""):
        raise AssertionError(f"yield message is not an operator handoff: {outcome.message}")
    return {
        "final": final.pos,
        "stalled": outcome.yielded_facts.stalled_steps,
        "failures": outcome.yielded_facts.failure_steps,
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
        spawn_or_fail(body, (0, 59, 0))
        command(rcon, f"tp {BOT} 0 59 0 -90 0")
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"effect clear {BOT}")
        command(rcon, "script in minebot run minebot_reset()")
        projection = asyncio.run(run_projection_refusal(body))

        command(rcon, "script in minebot run minebot_reset()")
        command(rcon, "fill -4 59 -4 20 66 8 air")
        command(rcon, "fill -4 58 -4 20 58 8 stone")
        command(rcon, f"tp {BOT} 0 59 0 -90 0")
        happy = asyncio.run(run_happy(body))

        command(rcon, "script in minebot run minebot_reset()")
        command(rcon, "fill -4 59 1 20 66 8 air")
        command(rcon, "fill -4 58 1 20 58 8 stone")
        command(rcon, "fill 4 59 3 4 62 7 stone")
        command(rcon, f"tp {BOT} 0 59 5 -90 0")
        failed = asyncio.run(run_yield(body))

        print({"projection": projection, "happy": happy, "yield": failed})


if __name__ == "__main__":
    main()
