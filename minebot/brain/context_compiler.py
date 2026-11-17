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
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from minebot.brain.modes import RuntimeProfile
from minebot.contract import BodyState

TASK_RUNTIME_CONTRACT_TEXT = (
    "TASK_RUNTIME_CONTRACT: The task goes on by itself when your turn ends: "
    "stop calling tools and say in a line or two what you did and what comes "
    "next; you rest until something wakes you. Call checkpoint_task only to wait for a named material event "
    "(wait_event), to put one grounded question to the player (yield), or to "
    "claim the goal complete with authoritative evidence (complete)."
)


@dataclass(frozen=True)
class ContextFacts:
    """Immutable snapshot of everything ``AgentContext`` has observed."""

    goal_text: str
    turn: int
    language: str
    goal_facts: Mapping[str, object] | None = None
    include_goal: bool = True
    include_session_messages: bool = True
    session_messages: tuple[tuple[str, str], ...] = ()
    task_artifact: Mapping[str, object] | None = None
    conversation_summary: Mapping[str, object] | None = None
    life: Mapping[str, object] | None = None
    ladder: str | None = None
    home: str | None = None
    body_state: BodyState | None = None
    nearby_entities: tuple[Mapping[str, object], ...] = ()
    profile: RuntimeProfile | None = None
    resume_facts: Mapping[str, object] | None = None
    continuous_tools: bool = False


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


def _render_goal_facts(facts: ContextFacts) -> str | None:
    """Render server-observed completion facts without prescribing an action."""

    if facts.goal_facts is None:
        return None
    return "GOAL_FACTS: " + json.dumps(
        facts.goal_facts, ensure_ascii=False, sort_keys=True
    )


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
    if facts.task_artifact is None or facts.continuous_tools:
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


def _render_ladder(facts: ContextFacts) -> str | None:
    # Already English prose, bounded by its writer (plan-ladder-20261028.md §7.5).
    return facts.ladder or None


def _render_home(facts: ContextFacts) -> str | None:
    # Already English prose, bounded by its writer (vision-and-home-20261028.md §7.7).
    return facts.home or None


def _render_life(facts: ContextFacts) -> str | None:
    if facts.life is None:
        return None
    return "LIFE: " + json.dumps(facts.life, ensure_ascii=False, sort_keys=True)


# A full player inventory holds at most 41 stacks.
INVENTORY_KINDS_SHOWN = 48
NOTABLE_MAX_CHARS = 400


def _entity_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _entity_distance(entity: Mapping[str, object], state: BodyState) -> float | None:
    for key in ("nearest_dist", "dist"):
        distance = _entity_number(entity.get(key))
        if distance is not None and distance >= 0:
            return distance
    squared = _entity_number(entity.get("dist2"))
    if squared is not None and squared >= 0:
        return math.sqrt(squared)
    pos = entity.get("pos")
    if isinstance(pos, (list, tuple)) and len(pos) == 3:
        numbers = [_entity_number(value) for value in pos]
        if all(value is not None for value in numbers):
            return math.dist(numbers, state.pos)
    return None


def _entity_label(value: object, limit: int = 48) -> str:
    return " ".join(str(value).split())[:limit]


def _render_notable(facts: ContextFacts) -> str | None:
    """Summarize the existing nearby sample, never infer a world-wide census."""
    state = facts.body_state
    if state is None:
        return None
    bosses: list[tuple[float, str]] = []
    crystals: dict[str, list[Mapping[str, object]]] = {}
    hostiles: dict[str, list[Mapping[str, object]]] = {}
    for entity in getattr(facts, "nearby_entities", ()):
        kind = _entity_label(entity.get("type") or entity.get("name") or "unknown")
        distance = _entity_distance(entity, state)
        if entity.get("boss") is True:
            detail = kind
            health = _entity_number(entity.get("health"))
            maximum = _entity_number(entity.get("max_health"))
            if health is not None:
                detail += f" health {health:g}"
                if maximum is not None:
                    detail += f"/{maximum:g}"
            if entity.get("phase") is not None:
                detail += " phase " + _entity_label(entity["phase"], 40)
            if distance is not None:
                detail += f" {distance:.0f} blocks"
            pos = entity.get("pos")
            if isinstance(pos, (list, tuple)) and len(pos) == 3 and all(
                _entity_number(value) is not None for value in pos
            ):
                detail += " pos=(" + ", ".join(str(math.floor(value)) for value in pos) + ")"
            bosses.append((distance if distance is not None else math.inf, detail))
        elif kind.removeprefix("minecraft:") == "end_crystal":
            crystals.setdefault(kind, []).append(entity)
        elif entity.get("hostile") is True or entity.get("aggressive") is True:
            hostiles.setdefault(kind, []).append(entity)

    parts = [detail for _, detail in sorted(bosses)]
    for kind, group in sorted(crystals.items()):
        detail = f"{kind} ×{len(group)}"
        cages = [entity.get("caged") for entity in group]
        if any(isinstance(caged, bool) for caged in cages):
            detail += f" ({sum(caged is True for caged in cages)} caged"
            if not all(isinstance(caged, bool) for caged in cages):
                detail += ", others unknown"
            detail += ")"
        distances = [_entity_distance(entity, state) for entity in group]
        known = [distance for distance in distances if distance is not None]
        if known:
            detail += f" nearest {min(known):.0f} blocks"
        parts.append(detail)
    if hostiles:
        groups = []
        for kind, group in sorted(hostiles.items()):
            detail = f"{kind} ×{len(group)}"
            angry = sum(entity.get("aggressive") is True for entity in group)
            if angry:
                detail += f" ({angry} aggressive)"
            elif all(entity.get("aggressive") is False for entity in group):
                detail += " (calm)"
            known = [d for d in (_entity_distance(entity, state) for entity in group) if d is not None]
            if known:
                detail += f" nearest {min(known):.0f} blocks"
            groups.append(detail)
        parts.append("hostile mobs: " + ", ".join(groups))
    if not parts:
        return None
    line = "NOTABLE (the Body's sample of nearest entities, not a full count): " + "; ".join(parts)
    if len(line) > NOTABLE_MAX_CHARS:
        line = line[:NOTABLE_MAX_CHARS - 1].rstrip() + "…"
    return line


def _render_body_state(facts: ContextFacts) -> str | None:
    state = facts.body_state
    if state is None:
        return None
    pos = ", ".join(f"{value:.1f}" for value in state.pos)
    line = (
        f"STATE: pos=({pos}) health={state.health:.1f} food={state.food} "
        f"dim={state.dimension or 'overworld'}"
    )
    # A player always knows what they carry. Read with the rest of the state
    # before each request, so it is never an older count than the tool results.
    counts = getattr(state, "inventory_counts", None)
    if isinstance(counts, dict):
        held = sorted((str(item), count) for item, count in counts.items() if isinstance(count, int) and count > 0)
        shown = ", ".join(f"{item} {count}" for item, count in held[:INVENTORY_KINDS_SHOWN]) or "empty"
        if len(held) > INVENTORY_KINDS_SHOWN:
            shown += f", and {len(held) - INVENTORY_KINDS_SHOWN} more kinds (read_inventory lists all)"
        line += f" inventory=[{shown}]"
    # What is worn and in the hands, apart from what is carried: the count
    # above holds worn armour too (body-tool-boundaries-20261110 §6).
    armor = getattr(state, "armor", None)
    if isinstance(armor, dict):
        worn = ", ".join(f"{slot} {armor.get(slot) or 'nothing'}" for slot in ("head", "chest", "legs", "feet"))
        hand = getattr(state, "selected_item", None) or "nothing"
        off = getattr(state, "offhand_item", None) or "nothing"
        line += f"\nWearing: {worn}; main hand {hand}; off hand {off}"
    # Uses left, as the durability bar shows it; the most worn first.
    wear = getattr(state, "tool_wear", None)
    if isinstance(wear, list) and wear:
        ranked = sorted(wear, key=lambda entry: entry["left"] / max(1, entry["max"]))
        line += "\nUses left: " + ", ".join(f"{entry['item']} {entry['left']} of {entry['max']}" for entry in ranked[:8])
    # How bright the spot is, as a player sees it. Raw levels, no verdict:
    # what darkness means is the Agent's to know (project-review-20261110 §9).
    light = getattr(state, "light", None)
    if isinstance(light, dict):
        line += (f"\nLight where the Body stands: {light['block']} of 15 from torches and other lit blocks, "
                 f"{light['sky']} of 15 of the sky reaches it; "
                 + ("open sky above" if light.get("open_sky") else "no open sky above"))
    walk_look_down = getattr(state, "walk_look_down", None)
    if walk_look_down:
        line += "\nWalking: looking down (your setting)"
    elif walk_look_down is False:
        line += "\nWalking: level (the last death turned your look-down setting off)"
    movement = getattr(state, "movement", None)
    if isinstance(movement, dict):
        if movement.get("mode") == "keep_moving":
            centre = [round(float(v)) for v in movement.get("center") or ()]
            line += (f"\nMovement: keeps moving around {centre} within {float(movement.get('radius') or 0):g} blocks "
                     "(your setting)")
        elif movement.get("reset_by_death"):
            line += "\nMovement: standing (the last death ended your keep_moving setting)"
    clock = _clock_line(state)
    if clock:
        line += "\n" + clock
    hurt = _last_hurt_line(getattr(state, "hurt_source", None), facts)
    if hurt:
        line += "\n" + hurt
    notable = _render_notable(facts)
    if notable:
        line += "\n" + notable
    return line


def _clock_line(state: BodyState) -> str | None:
    """The sky a player always sees, said as the clock (user 2026-11-09: the Agent
    understands from basic facts). Only the Overworld has a sun."""
    tick = getattr(state, "time", None)
    if not isinstance(tick, int) or isinstance(tick, bool):
        return None
    if str(state.dimension or "minecraft:overworld").removeprefix("minecraft:") != "overworld":
        return None
    tick %= 24000
    if tick < 12000 or tick >= 23000:
        to_sunset = (12000 - tick) % 24000
        return f"Clock: time {tick}, day; sunset at 12000, in about {round(to_sunset / 20)} seconds."
    if tick < 13000:
        return f"Clock: time {tick}, dusk; night falls at 13000, in about {round((13000 - tick) / 20)} seconds."
    return f"Clock: time {tick}, night; day comes at about 23000, in about {round((23000 - tick) / 20)} seconds."


def _count(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def _last_hurt_line(source: object, facts: ContextFacts | None = None) -> str | None:
    """The client's last damage event since respawn, as observed: when, by what, how far then.

    The distance is the one at the moment of the hit, and is said so; where that
    same mob is now is said beside it when the Body still sees it. run-36 27:00:
    "Last hurt: 7 seconds ago by Skeleton at 3 blocks" was read by the Agent as
    the skeleton standing 3 blocks off, while the nearest one was 18 blocks away;
    it went after it at 6.9 health and died (project-review-20261110 §9.16).
    """
    if not isinstance(source, Mapping):
        return None
    age = source.get("seconds_since_hurt")
    when = _count(round(age), "second") + " ago" if isinstance(age, (int, float)) and not isinstance(age, bool) else "at an unrecorded time"
    name = source.get("name") or source.get("type")
    kind = str(source.get("damage_type") or "").removeprefix("minecraft:")
    line = f"Last hurt: {when}"
    if name:
        line += f" by {name}" + (f" ({kind})" if kind else "")
        distance = source.get("distance")
        if isinstance(distance, (int, float)) and not isinstance(distance, bool):
            line += ", " + _count(round(distance), "block") + " away at that moment"
        now = _hurt_source_now(source, facts)
        if now is not None:
            line += f"; that {name} is {_count(round(now), 'block')} away now"
        elif source.get("id") and facts is not None and getattr(facts, "nearby_entities", ()):
            # Same sample as the NOTABLE line; without one, nothing is said.
            line += f"; that {name} is not among the entities the Body sees now"
        return line + "."
    return line + (f" ({kind})" if kind else "") + "."


def _hurt_source_now(source: Mapping[str, object], facts: ContextFacts | None) -> float | None:
    """Distance now to the very mob that dealt the last hurt, matched by id in the Body's sample."""
    source_id = source.get("id")
    state = getattr(facts, "body_state", None) if facts is not None else None
    if not source_id or state is None:
        return None
    for entity in getattr(facts, "nearby_entities", ()) or ():
        if isinstance(entity, Mapping) and entity.get("id") == source_id:
            return _entity_distance(entity, state)
    return None


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
    SectionSpec("goal_facts", 0, _render_goal_facts, provenance_required=True),
    SectionSpec("session_header", 0, _render_session_header),
    SectionSpec("session_messages", 3, _render_session_messages),
    SectionSpec("task_facts", 1, _render_task_artifact, provenance_required=True),
    SectionSpec("task_runtime_contract", 1, _render_task_runtime_contract),
    SectionSpec(
        "conversation_summary", 3, _render_conversation_summary, provenance_required=True
    ),
    SectionSpec("life", 2, _render_life, provenance_required=True),
    SectionSpec("ladder", 2, _render_ladder, provenance_required=True),
    SectionSpec("home", 2, _render_home, provenance_required=True),
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
        {"goal", "session_header", "session_messages", "conversation_summary", "life", "ladder", "home",
         "body_state"}
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
