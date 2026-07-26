"""F2 Context Compiler — declarative section pipeline over observed facts.

brain-cognitive-framework.md §5. The compiler is a pure function: given an
immutable ``ContextFacts`` snapshot, a profile name, and an optional budget,
it produces the exact turn preamble plus telemetry. ``AgentContext`` stays
the stateful owner/recorder (observe_* API unchanged) and delegates its
assembly here, so the ``full`` profile is byte-identical to the historical
``turn_preamble`` output (golden-locked in
``tests/unit/test_context_compiler.py``).

Rules (§5.2):

- Assembly order is the section registry order; ``priority`` governs only
  budget drops (0 = never dropped).
- A profile is a named subset of sections. ``full`` is everything; ``terse``
  keeps priority ≤ 1 (the r50b mobility lesson, generalized); ``social`` and
  ``maintenance`` are declared here but only routed once F1 activation flows
  a ``context_profile`` other than ``full``.
- Budget overflow drops WHOLE sections, highest priority number first
  (ties: later registry position drops first). Priority-0 sections are never
  dropped; if they alone still exceed the budget the result is marked
  ``truncated`` rather than cut mid-fact.
- ``resume_facts`` is a one-shot interruption frame: the facade consumes it
  only when it was actually rendered, so a profile that omits it cannot
  silently destroy the fact.

``staleness`` / ``provenance_required`` are declared per section now; their
enforcement (re-grounding cadence) wires up with P1 activation and restart
reconciliation, not here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from minebot.brain.modes import RuntimeProfile
from minebot.contract import BodyState

TASK_RUNTIME_CONTRACT_TEXT = (
    "TASK_RUNTIME_CONTRACT: A durable task spans finite SDK runs only "
    "through checkpoint_task. Before final output, record exactly one "
    "explicit disposition: continue with a structured continuation when "
    "the unfinished goal remains actionable; wait_event only for a named "
    "material wake condition; yield only for a grounded bounded blocker; "
    "complete only with authoritative evidence."
)


@dataclass(frozen=True)
class ContextFacts:
    """Immutable snapshot of everything ``AgentContext`` has observed."""

    goal_text: str
    turn: int
    language: str
    include_goal: bool = True
    include_session_messages: bool = True
    session_messages: tuple[tuple[str, str], ...] = ()
    task_artifact: Mapping[str, object] | None = None
    conversation_summary: Mapping[str, object] | None = None
    body_state: BodyState | None = None
    profile: RuntimeProfile | None = None
    resume_facts: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SectionSpec:
    section_id: str
    priority: int                                # 0 = never dropped
    render: Callable[[ContextFacts], str | None]  # None = omit this turn
    staleness: str = "per_turn"                   # declared; enforced at P1
    provenance_required: bool = False


@dataclass(frozen=True)
class ContextBudget:
    max_chars: int | None = None


@dataclass(frozen=True)
class CompiledContext:
    text: str
    profile: str
    sections: tuple[str, ...]   # rendered section ids, assembly order
    dropped: tuple[str, ...]    # rendered but removed by budget, drop order
    excluded: tuple[str, ...]   # excluded by the profile
    chars: int
    truncated: bool             # priority-0 alone still exceeded the budget


# -- section renderers (must reproduce historical lines exactly) -----------


def _render_goal(facts: ContextFacts) -> str | None:
    if facts.include_goal and facts.goal_text.strip():
        return f"GOAL: {facts.goal_text}"
    return None


def _render_session_header(facts: ContextFacts) -> str | None:
    return f"SESSION: turn={facts.turn} language={facts.language}"


def _render_session_messages(facts: ContextFacts) -> str | None:
    if not facts.include_session_messages or not facts.session_messages:
        return None
    chunks = [f"{role}: {text}" for role, text in facts.session_messages]
    return "SESSION_MESSAGES: " + " | ".join(chunks)


def _render_task_artifact(facts: ContextFacts) -> str | None:
    if facts.task_artifact is None:
        return None
    return "TASK_ARTIFACT: " + json.dumps(
        facts.task_artifact, ensure_ascii=False, sort_keys=True
    )


def _render_task_runtime_contract(facts: ContextFacts) -> str | None:
    if facts.task_artifact is None:
        return None
    task = facts.task_artifact.get("task")
    if isinstance(task, dict) and task.get("status") == "running":
        return TASK_RUNTIME_CONTRACT_TEXT
    return None


def _render_conversation_summary(facts: ContextFacts) -> str | None:
    summary = facts.conversation_summary
    if summary is not None and summary.get("compacted_turns", 0):
        return "CONVERSATION_SUMMARY: " + json.dumps(
            summary, ensure_ascii=False, sort_keys=True
        )
    return None


def _render_body_state(facts: ContextFacts) -> str | None:
    state = facts.body_state
    if state is None:
        return None
    pos = ", ".join(f"{value:.1f}" for value in state.pos)
    return (
        f"STATE: pos=({pos}) health={state.health:.1f} food={state.food} "
        f"dim={state.dimension or 'overworld'}"
    )


def _render_profile(facts: ContextFacts) -> str | None:
    profile = facts.profile
    if profile is None:
        return None
    focus = ",".join(profile.tool_focus)
    tags = ",".join(profile.policy_tags)
    return (
        f"PROFILE: relationship={profile.relationship} situational={profile.situational} "
        f"lifecycle={profile.lifecycle} focus={focus} model={profile.model_route} "
        f"effort={profile.effort} policy={tags} frame={profile.context_frame}"
    )


def _render_resume(facts: ContextFacts) -> str | None:
    resume = facts.resume_facts
    if resume is None:
        return None
    reason = resume.get("reason") or "resume"
    goal = resume.get("goal") or ""
    progress = resume.get("last_progress") or {}
    return f"RESUME: reason={reason} goal={goal} last_progress={progress}"


# Registry order = assembly order (historical turn_preamble order).
# ``resume_facts`` is priority 1, not 2: it is a one-shot interruption fact
# whose loss is permanent, so only a deliberate profile exclusion — never a
# budget squeeze past the P1 band — may remove it.
SECTION_REGISTRY: tuple[SectionSpec, ...] = (
    SectionSpec("goal", 0, _render_goal),
    SectionSpec("session_header", 0, _render_session_header),
    SectionSpec("session_messages", 3, _render_session_messages),
    SectionSpec("task_facts", 1, _render_task_artifact, provenance_required=True),
    SectionSpec("task_runtime_contract", 1, _render_task_runtime_contract),
    SectionSpec(
        "conversation_summary", 3, _render_conversation_summary, provenance_required=True
    ),
    SectionSpec("body_state", 0, _render_body_state),
    SectionSpec("stance_profile", 1, _render_profile),
    SectionSpec("resume_facts", 1, _render_resume),
)

_ALL_SECTION_IDS = tuple(spec.section_id for spec in SECTION_REGISTRY)

CONTEXT_PROFILES: Mapping[str, frozenset[str]] = {
    "full": frozenset(_ALL_SECTION_IDS),
    # r50b generalized: mobility turns keep only the load-bearing band.
    "terse": frozenset(
        spec.section_id for spec in SECTION_REGISTRY if spec.priority <= 1
    ),
    # Conversational turns without a durable goal: dialogue framing, no task
    # contract or stance machinery.
    "social": frozenset(
        {"goal", "session_header", "session_messages", "conversation_summary", "body_state"}
    ),
    # Reflection/distillation: durable artifacts and summaries, no live
    # dialogue window or stance frame.
    "maintenance": frozenset(
        {"goal", "session_header", "task_facts", "conversation_summary", "body_state"}
    ),
}


def compile_context(
    profile: str,
    facts: ContextFacts,
    budget: ContextBudget | None = None,
) -> CompiledContext:
    """Compile the turn preamble for one profile. Pure and deterministic."""

    try:
        enabled = CONTEXT_PROFILES[profile]
    except KeyError:
        known = ", ".join(sorted(CONTEXT_PROFILES))
        raise ValueError(f"unknown context profile {profile!r}; known: {known}") from None

    excluded = tuple(
        spec.section_id for spec in SECTION_REGISTRY if spec.section_id not in enabled
    )
    rendered: list[tuple[SectionSpec, str]] = []
    for spec in SECTION_REGISTRY:
        if spec.section_id not in enabled:
            continue
        text = spec.render(facts)
        if text is not None:
            rendered.append((spec, text))

    dropped: list[str] = []
    truncated = False
    max_chars = budget.max_chars if budget is not None else None
    if max_chars is not None:
        while _joined_chars(rendered) > max_chars:
            candidate_index = _drop_candidate_index(rendered)
            if candidate_index is None:
                truncated = True
                break
            spec, _ = rendered.pop(candidate_index)
            dropped.append(spec.section_id)

    text = "\n".join(part for _, part in rendered)
    return CompiledContext(
        text=text,
        profile=profile,
        sections=tuple(spec.section_id for spec, _ in rendered),
        dropped=tuple(dropped),
        excluded=excluded,
        chars=len(text),
        truncated=truncated,
    )


def _joined_chars(rendered: list[tuple[SectionSpec, str]]) -> int:
    if not rendered:
        return 0
    return sum(len(part) for _, part in rendered) + len(rendered) - 1


def _drop_candidate_index(rendered: list[tuple[SectionSpec, str]]) -> int | None:
    """Highest priority number drops first; ties drop the later section."""

    best_index: int | None = None
    best_priority = 0
    for index, (spec, _) in enumerate(rendered):
        if spec.priority <= 0:
            continue
        if spec.priority >= best_priority:
            best_priority = spec.priority
            best_index = index
    return best_index


__all__ = [
    "CONTEXT_PROFILES",
    "CompiledContext",
    "ContextBudget",
    "ContextFacts",
    "SECTION_REGISTRY",
    "SectionSpec",
    "TASK_RUNTIME_CONTRACT_TEXT",
    "compile_context",
]
