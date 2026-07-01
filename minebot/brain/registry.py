"""Tool registry + progress weld — owned seams ① and ② (agent core).

This module is the single source of truth for tools and the mandatory progress
weld every Body-mutating tool call passes through. It is framework-agnostic:
it imports only ``minebot.contract`` and the sibling ``ProgressAuthority``; it
never imports the agent SDK, transport (``game/``), or Body transaction
implementations (``body/``). The Body is reached only through the neutral
``Body`` protocol from ``contract/``.

Two designs live here:

- **Registry (seam ①).** One registration emits two faces:
  *framework face* (``name`` / ``description`` / ``input_schema`` — all the LLM
  and SDK see) and the *governance sidecar* (``permission`` / ``progress_key`` /
  ``body_scope`` / ``terminal_truth`` / ``mutating`` / ``timeout_s`` — owned by
  the registry, never shown to the model). See
  ``body-agent-interface-schema.md`` §3.

- **Progress weld (seam ②).** Routes every mutating tool call through the one
  ``ProgressAuthority`` so stall / stagnation / failure-storm detection cannot be
  bypassed — the discipline the SDK's Runner lacks. See
  ``agent-layer-architecture.md`` §5.2 and ``body-agent-interface-schema.md`` §8.

The weld's runtime dependencies (the live ``Body``, the shared
``ProgressAuthority``, the current goal text, the per-bot single-writer guard)
are **injected at call time** via :class:`WeldContext`. The registry holds none
of them, so it stays a pure data structure that the composition root wires up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from minebot.brain.progress import ProgressAuthority
from minebot.contract import Body, JsonObject, ProgressAbort, ToolResult, is_candidate_skip

# A tool's callable: takes validated input params, returns a ToolResult whose
# success reflects *verified Body terminal truth* (the transaction awaits its own
# terminal event; the weld does not poll for it). Schema validation happens at
# the framework face (SDK function_tool), not here.
ToolCallable = Callable[[JsonObject], ToolResult]

# Neutral sentinel for a stale-generation completion: truthy but not success,
# not a failure, and not appended as fresh world truth (schema §8).
PREEMPTED_PAYLOAD: JsonObject = ToolResult(
    success=True, reason="preempted", can_retry=True, metrics={"paused": True}
).to_payload()

PROGRESS_YIELDED_REASON = "progress_yielded"


@dataclass(frozen=True)
class ToolSidecar:
    """Governance/progress metadata owned by the registry, never shown to the LLM.

    ``permission`` / ``body_scope`` / ``terminal_truth`` are carried for the
    second-net guardrail and the observability layer; the weld itself only acts
    on ``progress_key`` and ``mutating``. ``timeout_s`` is declarative here — the
    transaction enforces its own terminal timeout; the async runner may also wrap
    the call in ``wait_for(timeout_s)``.
    """

    progress_key: str
    mutating: bool
    source: str = "unknown"
    tool_type: str = "general"
    permission: str = "none"
    body_scope: tuple[str, ...] = ()
    terminal_truth: tuple[str, ...] = ()
    timeout_s: float | None = None


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    input_schema: JsonObject
    callable: ToolCallable
    sidecar: ToolSidecar

    def framework_view(self) -> JsonObject:
        """The only fields the agent framework / LLM may see."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    """Single source of truth for tools; emits framework + sidecar faces."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> RegisteredTool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        if not tool.sidecar.progress_key:
            raise ValueError(f"tool {tool.name!r}: sidecar.progress_key must be non-empty")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError:
            known = ", ".join(sorted(self._tools)) or "<none>"
            raise KeyError(f"unknown tool {name!r}; registered: {known}") from None

    def sidecar(self, name: str) -> ToolSidecar:
        return self.get(name).sidecar

    def framework_tools(self) -> list[JsonObject]:
        """name/description/input_schema only — what the runner turns into
        SDK function tools."""
        return [tool.framework_view() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


class SingleWriterGuard:
    """Per-bot single-writer for mutating tools (schema §8).

    Non-blocking ``try_acquire``: a second mutating tool for the same bot while
    one is in flight is rejected with stable owner/busy truth rather than racing
    the Body owner. This synchronous guard proves the rejection logic; the async
    runner layers an ``asyncio.Lock`` on top for true concurrency serialization.
    """

    def __init__(self) -> None:
        self._holder: str | None = None

    @property
    def holder(self) -> str | None:
        return self._holder

    def try_acquire(self, progress_key: str) -> bool:
        if self._holder is not None:
            return False
        self._holder = progress_key
        return True

    def release(self, progress_key: str) -> None:
        if self._holder == progress_key:
            self._holder = None


@dataclass
class WeldContext:
    """Per-run runtime injected into the weld by the composition root.

    ``goal_text`` is intentionally mutable: ``AgentContext`` owns the goal and
    re-injects it on a cadence, so the weld reads the *current* goal each call.
    """

    body: Body
    authority: ProgressAuthority
    goal_text: str
    writer: SingleWriterGuard = field(default_factory=SingleWriterGuard)


def _action_key(progress_key: str, tool_input: JsonObject) -> tuple[Any, ...]:
    """Stable identity for stagnation detection (same action repeated)."""
    try:
        signature = json.dumps(tool_input, sort_keys=True, default=str)
    except TypeError:
        signature = repr(sorted(tool_input.items()))
    return (progress_key, signature)


def execute_tool(tool: RegisteredTool, tool_input: JsonObject, ctx: WeldContext) -> JsonObject:
    """Run one tool through the progress weld; return a ToolResult payload.

    Non-mutating tools (read/perception) bypass progress accounting and the
    single-writer — they are concurrent observations, not progress steps
    (schema §8). Mutating tools run the full 7-step weld and may raise
    ``ProgressAbort`` (surfaced upstream as a yield, never a crash).
    """
    sidecar = tool.sidecar

    # Read-only path: concurrent observation, no single-writer. Body state /
    # perception reads still feed stagnation/stall sensors so tool-only
    # observation loops cannot run forever. Agent composition tools stay leaf-led
    # per phase1 design: their inner Body calls own progress accounting.
    if not sidecar.mutating:
        result = tool.callable(tool_input)
        if sidecar.source.startswith("body."):
            fingerprint = ctx.authority.fingerprint(ctx.body.get_state())
            ctx.authority.observe_step(_action_key(sidecar.progress_key, tool_input), fingerprint)
            ctx.authority.require_can_continue(ctx.goal_text)
        return result.to_payload()

    # Mutating path — single-writer first (schema §8).
    if not ctx.writer.try_acquire(sidecar.progress_key):
        return ToolResult(
            success=False,
            reason="owner_busy",
            can_retry=True,
            metrics={"holder": ctx.writer.holder, "requested": sidecar.progress_key},
        ).to_payload()

    try:
        generation = ctx.authority.next_generation()      # step 2: generation
        ctx.authority.fingerprint(ctx.body.get_state())   # step 1: pre fingerprint (validated)
        result = tool.callable(tool_input)                # steps 3-4: execute + await terminal

        # step 5 (stale-generation): a higher-priority owner superseded us mid
        # call. Neutral preempted — not noted as fresh world truth (schema §8).
        if not ctx.authority.generation_current(generation):
            return PREEMPTED_PAYLOAD

        # Some long Body transactions feed intermediate movement/combat steps
        # into this same ProgressAuthority. If that inner controller already
        # yielded, surface the yield directly instead of counting the wrapper
        # call as another failed tool result.
        if result.reason == PROGRESS_YIELDED_REASON:
            facts = ctx.authority.facts(ctx.goal_text)
            raise ProgressAbort(
                "progress authority yielded: "
                f"goal={facts.goal!r} stagnant={facts.stagnant_steps} "
                f"stalled={facts.stalled_steps} failures={facts.failure_steps}",
                facts=facts,
            )

        post_fingerprint = ctx.authority.fingerprint(ctx.body.get_state())  # step 5: post fp
        action_key = _action_key(sidecar.progress_key, tool_input)
        # A candidate-skip ("this target is unsuitable, pick another") is neutral:
        # not progress, not failure (agent-loop.md §6). Without this, a composition
        # probing a messy candidate field trips the failure storm on healthy work.
        neutral = result.reason == "preempted" or is_candidate_skip(result.reason)
        ctx.authority.note_step(                          # step 6: feed sensors
            action_key, success=result.success, fingerprint=post_fingerprint, neutral=neutral
        )
        ctx.authority.require_can_continue(ctx.goal_text)  # step 7: trip -> ProgressAbort
        return result.to_payload()
    finally:
        ctx.writer.release(sidecar.progress_key)


__all__ = [
    "ToolCallable",
    "ToolSidecar",
    "RegisteredTool",
    "ToolRegistry",
    "SingleWriterGuard",
    "WeldContext",
    "execute_tool",
    "PREEMPTED_PAYLOAD",
    "ProgressAbort",
]
