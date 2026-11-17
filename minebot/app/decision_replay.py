"""Replay decision-corpus fixtures against a configured provider (F7).

brain-cognitive-framework.md §10.1: a model/provider/prompt change is
measured by replaying the corpus and diffing chosen tool calls per decision
context BEFORE any live run is spent on it.

Honesty notes:

- The model is never told it is being replayed; the fixture's recorded
  context is presented as ordinary instructions, mirroring the production
  instruction shape (persona + context preamble + continuation input).
- Harvested pre-F2 fixtures carry the *recorded decision facts* rather than
  the exact compiled instructions, so absolute drift numbers on old packs
  are approximations; compare models against each other on the same pack
  rather than reading a single absolute rate. Once live runs record
  F2-compiled contexts, replays become exact.
- Replay tools are schema-true stubs: the model sees the real framework
  faces (name/description/schema) of the current registry, and
  ``stop_on_first_tool`` ends the turn at the first tool batch without any
  Body side effects.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from agents import Agent, RunConfig, Runner
from agents.tool import FunctionTool

from minebot.app.conversation import bounded_session_input
from minebot.app.model_provider import ModelProviderRegistry
from minebot.brain.metacognition import DecisionFixture, ReplayedDecision
from minebot.brain.persona import MINEBOT_SYSTEM_PROMPT
from minebot.brain.registry import ToolRegistry

RunnerCallable = Callable[..., Any]

_REPLAY_INPUT = "Continue the current goal from the latest authoritative state."


async def _stub_invoke(_ctx: Any, _input_json: str) -> str:
    return json.dumps({"success": True, "reason": "replay_stub"})


def stub_tools_from_registry(registry: ToolRegistry) -> list[FunctionTool]:
    """Schema-true stub tools: real framework faces, inert callables."""

    tools: list[FunctionTool] = []
    for view in registry.framework_tools():
        tools.append(
            FunctionTool(
                name=str(view["name"]),
                description=str(view["description"]),
                params_json_schema=dict(view["input_schema"]),  # type: ignore[arg-type]
                on_invoke_tool=_stub_invoke,
                strict_json_schema=False,
            )
        )
    return tools


class ReplayEngine:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        model_provider: ModelProviderRegistry | None = None,
        logical_model: str = "primary",
        system_prompt: str = MINEBOT_SYSTEM_PROMPT,
        runner_run: RunnerCallable | None = None,
    ) -> None:
        self.model_provider = model_provider
        self.logical_model = logical_model
        self.system_prompt = system_prompt
        self.runner_run: RunnerCallable = runner_run or Runner.run
        self._tools = stub_tools_from_registry(registry)

    async def replay(self, fixture: DecisionFixture) -> ReplayedDecision:
        agent = Agent(
            name="MineBotReplay",
            instructions=f"{self.system_prompt}\n\n{fixture.compiled_context}",
            tools=list(self._tools),
            model=self.logical_model,
            tool_use_behavior="stop_on_first_tool",
        )
        run_config = (
            RunConfig(session_input_callback=bounded_session_input)
            if self.model_provider is None
            else RunConfig(
                model_provider=self.model_provider,
                model_settings=self.model_provider.model_settings_for(self.logical_model),
                session_input_callback=bounded_session_input,
            )
        )
        try:
            result = await self.runner_run(
                agent,
                _REPLAY_INPUT,
                run_config=run_config,
                max_turns=2,
            )
        except Exception as exc:  # provider/transport failures are data, not crashes
            return ReplayedDecision(
                fixture_id=fixture.fixture_id,
                chosen=(),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
        return ReplayedDecision(
            fixture_id=fixture.fixture_id,
            chosen=tuple(_extract_tool_calls(result)),
        )

    async def replay_all(
        self, fixtures: Iterable[DecisionFixture]
    ) -> dict[str, ReplayedDecision]:
        replays: dict[str, ReplayedDecision] = {}
        for fixture in fixtures:
            replays[fixture.fixture_id] = await self.replay(fixture)
        return replays


def _extract_tool_calls(result: Any) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for item in getattr(result, "new_items", []) or []:
        raw = getattr(item, "raw_item", item)
        raw_type = getattr(raw, "type", None) or (
            raw.get("type") if isinstance(raw, dict) else None
        )
        if raw_type != "function_call":
            continue
        name = getattr(raw, "name", None) or (
            raw.get("name") if isinstance(raw, dict) else None
        )
        arguments = getattr(raw, "arguments", None)
        if arguments is None and isinstance(raw, dict):
            arguments = raw.get("arguments")
        calls.append({"tool": name, "arguments": arguments})
    return calls


__all__ = ["ReplayEngine", "stub_tools_from_registry"]
