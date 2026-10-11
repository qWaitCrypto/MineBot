"""Framework-neutral tool registration, single-writer control and result delivery.

Progress observations are advisory. They cannot veto a new action or replace
its actual result; cancellation generations still reject stale completions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from minebot.brain.progress import ProgressAuthority
from minebot.contract import (
    Body,
    BodyState,
    JsonObject,
    ToolResult,
    is_candidate_skip,
    is_governance_decision_required,
    is_partial_progress,
)

# A tool's callable: takes validated input params, returns a ToolResult whose
# success reflects *verified Body terminal truth* (the transaction awaits its own
# terminal event; the weld does not poll for it). Schema validation happens at
# the framework face (SDK function_tool), not here.
ToolCallable = Callable[[JsonObject], ToolResult]

# Optional per-tool observation projector: (reason, metrics) -> bounded summary
# facts for the model-visible payload. Registered NEXT TO the tool it projects
# (brain-cognitive-framework.md §12 H1) so the framework binding stays
# capability-free: the runner threads the projector through; it never owns
# per-tool projection knowledge. Full metrics remain persisted/queryable
# regardless of what the projector surfaces.
ObservationProjector = Callable[[str, dict[str, object]], JsonObject]

# A bounded, evidence-only read used by the generic long-call supervisor when
# the execution snapshot cannot answer a doubt.  It is deliberately a reader,
# not a detector or a second execution path; its result is merged back into the
# call's execution snapshot.
SupervisionReader = Callable[[JsonObject], object]

# Neutral sentinel for a stale-generation completion: truthy but not success,
# not a failure, and not appended as fresh world truth (schema §8).
PREEMPTED_PAYLOAD: JsonObject = ToolResult(
    success=False, reason="preempted", can_retry=True, metrics={"paused": True}
).to_payload()

PROGRESS_YIELDED_REASON = "progress_yielded"


@dataclass(frozen=True)
class ToolSidecar:
    """Governance/progress metadata owned by the registry, never shown to the LLM.

    ``permission`` / ``body_scope`` / ``terminal_truth`` are carried for the
    second-net guardrail and the observability layer; the weld itself only acts
    on ``progress_key`` and ``mutating``. ``body_mutating`` is separate because
    a composition can be leaf-led for progress accounting while still changing
    the physical Body. ``timeout_s`` is declarative here. The transaction owns
    its terminal timeout, while the runner enforces the outer tool budget only
    after the call enters the serialized execution lane.
    """

    progress_key: str
    mutating: bool
    source: str = "unknown"
    tool_type: str = "general"
    permission: str = "none"
    body_scope: tuple[str, ...] = ()
    terminal_truth: tuple[str, ...] = ()
    timeout_s: float | None = None
    body_mutating: bool | None = None
    model_visible: bool = True

    @property
    def can_mutate_body(self) -> bool:
        if self.body_mutating is not None:
            return self.body_mutating
        return self.mutating


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    input_schema: JsonObject
    callable: ToolCallable
    sidecar: ToolSidecar
    projector: ObservationProjector | None = None
    supervision_explanation: str | None = None
    supervision_reader: SupervisionReader | None = None

    def framework_view(self) -> JsonObject:
        """The only fields the agent framework / LLM may see."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class CanonicalToolBinding:
    """One model-visible intent bound to exactly one runtime implementation."""

    intent: str
    tool_name: str
    implementation: str


class ToolRegistry:
    """Single source of truth for tools; emits framework + sidecar faces."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._canonical_bindings: dict[str, CanonicalToolBinding] = {}

    def register(
        self,
        tool: RegisteredTool,
        *,
        intent: str | None = None,
        implementation: str | None = None,
    ) -> RegisteredTool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        if not tool.sidecar.progress_key:
            raise ValueError(f"tool {tool.name!r}: sidecar.progress_key must be non-empty")
        if (intent is None) != (implementation is None):
            raise ValueError("canonical tool registration requires both intent and implementation")
        if intent is not None:
            if not intent.strip():
                raise ValueError("canonical tool intent must be non-empty")
            if not implementation or not implementation.strip():
                raise ValueError(f"canonical tool {intent!r}: implementation must be non-empty")
            if intent in self._canonical_bindings:
                previous = self._canonical_bindings[intent]
                raise ValueError(
                    f"duplicate canonical intent {intent!r}: "
                    f"{previous.tool_name!r} and {tool.name!r}"
                )
            self._canonical_bindings[intent] = CanonicalToolBinding(
                intent=intent,
                tool_name=tool.name,
                implementation=implementation,
            )
        if tool.sidecar.can_mutate_body and not tool.supervision_explanation:
            tool = replace(
                tool,
                supervision_explanation=(
                    "The Body normally carries out the declared physical action through its native process, "
                    "reports real progress and phase changes, and returns one authoritative terminal result."
                ),
            )
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
        return [self._tools[name].framework_view() for name in self.model_names()]

    def model_names(self) -> list[str]:
        """Tools exposed to the model; internal composition leaves stay registered."""
        return [name for name, tool in self._tools.items() if tool.sidecar.model_visible]

    def names(self) -> list[str]:
        return list(self._tools)

    def canonical_bindings(self) -> tuple[CanonicalToolBinding, ...]:
        return tuple(self._canonical_bindings.values())

    def canonical_binding(self, intent: str) -> CanonicalToolBinding:
        try:
            return self._canonical_bindings[intent]
        except KeyError:
            known = ", ".join(sorted(self._canonical_bindings)) or "<none>"
            raise KeyError(f"unknown canonical intent {intent!r}; registered: {known}") from None

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
    body_state_observer: Callable[[BodyState, str], None] | None = None
    raw_result_observer: Callable[[str, JsonObject, ToolResult], None] | None = None


def _action_key(progress_key: str, tool_input: JsonObject) -> tuple[Any, ...]:
    """Stable identity for stagnation detection (same action repeated)."""
    try:
        signature = json.dumps(tool_input, sort_keys=True, default=str)
    except TypeError:
        signature = repr(sorted(tool_input.items()))
    return (progress_key, signature)


def execute_tool(tool: RegisteredTool, tool_input: JsonObject, ctx: WeldContext) -> JsonObject:
    """Return the tool's receipt, preserving single-writer and cancel semantics."""
    sidecar = tool.sidecar

    def payload(value: Any) -> JsonObject:
        """Normalize framework callables at the registry boundary.

        Most Body tools return ``ToolResult``.  Management tools are pure
        runtime controls and naturally return an already serialized mapping;
        accepting both here keeps every model-visible tool on the same path.
        """
        if isinstance(value, ToolResult):
            return value.to_payload()
        if isinstance(value, dict):
            return dict(value)
        raise TypeError(
            f"tool {tool.name!r} returned {type(value).__name__}; expected ToolResult or mapping"
        )

    # Reading new world information need not change position or inventory.
    if not sidecar.mutating:
        return payload(tool.callable(tool_input))

    # Mutating path — single-writer first (schema §8).
    if not ctx.writer.try_acquire(sidecar.progress_key):
        return ToolResult(
            success=False,
            reason="owner_busy",
            can_retry=True,
            metrics={"holder": ctx.writer.holder, "requested": sidecar.progress_key},
        ).to_payload()

    try:
        from minebot.contract.execution import current_claim_generation
        claimed_generation = current_claim_generation()
        generation = (ctx.authority.next_generation() if claimed_generation is None
                      else claimed_generation)
        if not ctx.authority.generation_current(generation):
            return dict(PREEMPTED_PAYLOAD)
        ctx.authority.fingerprint(ctx.body.get_state())   # step 1: pre fingerprint (validated)
        result = tool.callable(tool_input)                # steps 3-4: execute + await terminal

        if ctx.raw_result_observer is not None:
            try:
                ctx.raw_result_observer(tool.name, tool_input, result)
            except Exception:
                # Telemetry failure cannot erase the received physical result.
                pass

        # Capture delivery after the authoritative generation check and before
        # the optional post-state read. A successful physical terminal remains
        # delivered even when the follow-up state sample is unavailable.
        from minebot.contract.execution import current_execution_receipt
        receipt = current_execution_receipt()

        # Record current server truth immediately after every physical leaf.
        # Composition tools call this same weld, so inventory/world progress is
        # visible at the moment it happens instead of at a later model turn.
        post_state_error = None
        try:
            post_state = ctx.body.get_state()
            if ctx.body_state_observer is not None:
                ctx.body_state_observer(post_state, f"mutation:{tool.name}")
        except Exception as exc:
            post_state = None
            post_state_error = type(exc).__name__

        # step 5 (stale-generation): a higher-priority owner superseded us mid
        # call. Neutral preempted — not noted as fresh world truth (schema §8).
        if not ctx.authority.generation_current(generation):
            return {**PREEMPTED_PAYLOAD, "metrics": {
                "original_result": result.to_payload(), "preempted": True,
                "post_state_unknown": post_state_error,
            }}

        if receipt is not None and result.success and result.reason != "preempted":
            receipt["delivered"] = True
            receipt["tool"] = tool.name

        if post_state is None:
            return {**result.to_payload(), "post_state_unknown": post_state_error}
        post_fingerprint = ctx.authority.fingerprint(post_state)  # step 5: post fp
        action_key = _action_key(sidecar.progress_key, tool_input)
        # A candidate-skip ("this target is unsuitable, pick another") is neutral:
        # not progress, not failure (agent-loop.md §6). Without this, a composition
        # probing a messy candidate field trips the failure storm on healthy work.
        neutral = (
            result.reason == "preempted"
            or is_candidate_skip(result.reason)
            or is_governance_decision_required(result.reason, result.metrics)
            or is_partial_progress(result.reason, result.metrics)
        )
        ctx.authority.note_step(                          # step 6: feed sensors
            action_key, success=result.success, fingerprint=post_fingerprint, neutral=neutral
        )
        return result.to_payload()
    finally:
        ctx.writer.release(sidecar.progress_key)


__all__ = [
    "ObservationProjector",
    "ToolCallable",
    "ToolSidecar",
    "CanonicalToolBinding",
    "RegisteredTool",
    "ToolRegistry",
    "SingleWriterGuard",
    "WeldContext",
    "execute_tool",
    "PREEMPTED_PAYLOAD",
]
