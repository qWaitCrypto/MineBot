"""The reflex frame question: Body facts as plain sentences, options as concrete things to do.

This is the only place that writes question text for Decision.  The wording
was settled offline against recorded frames (see
``docs/design-docs/reflex-frame-question-20261012.md``); change it there first.
The module is pure: callers hand in raw facts and get back a request body.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# Options that protect the Body.  They are asked in a separate question and
# only used when Decision says the Body is in danger.
PROTECTIVE = ("surface", "move_away", "retreat_safe")
# A hostile mob farther than this cannot be escaped by one short move.
HOSTILE_REACH = 16.0
AIR_FULL = 300
# A health change smaller than this is regeneration noise, not an injury.
HURT_MARGIN = 0.4
VITAL_BASELINE_S = (3.0, 8.0)

_ARGS_NOT_SHOWN = {"timeout_s", "segment_timeout_s", "max_segments", "intent"}
_DANGER = {
    "safe": "Nothing is harming the Body and nothing nearby is about to harm it.",
    "in_danger": "The Body is being harmed right now, or something nearby is about to harm it.",
}


def call_text(tool: str, params: Mapping[str, Any] | None = None) -> str:
    """Write a call the way the Agent would read it, without transport arguments."""
    shown = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in (params or {}).items() if key not in _ARGS_NOT_SHOWN
    )
    return f"{tool}({shown})"


class VitalWindow:
    """Recent health and air samples for the whole Body, not for one call."""

    def __init__(self) -> None:
        self._samples: list[tuple[float, dict[str, float]]] = []

    def add(self, now: float, facts: Mapping[str, Any]) -> dict[str, Any]:
        """Record this frame and return the values from a few seconds ago."""
        sample = {key: float(facts[key]) for key in ("health", "oxygen")
                  if isinstance(facts.get(key), (int, float))}
        before: dict[str, Any] = {}
        oldest, newest = VITAL_BASELINE_S[1], VITAL_BASELINE_S[0]
        for stamp, values in self._samples:
            if newest <= now - stamp <= oldest:
                before = {f"{key}_before": value for key, value in values.items()}
                before["vital_age_s"] = now - stamp
                break
        if sample:
            self._samples.append((now, sample))
        self._samples = [(stamp, values) for stamp, values in self._samples if now - stamp <= oldest]
        return before


def hostiles(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Hostile mobs in the frame, nearest first, each with ``name`` and ``dist``."""
    found: list[dict[str, Any]] = []
    for key in ("nearby_hostiles", "hostiles", "nearby_entities", "entities", "entities_near"):
        items = facts.get(key)
        if isinstance(items, Mapping):
            items = items.get("entities", items.get("items"))
        if not isinstance(items, (list, tuple)):
            continue
        for item in items:
            if not isinstance(item, Mapping) or not item.get("hostile"):
                continue
            dist = item.get("dist", item.get("distance"))
            if dist is None and isinstance(item.get("dist2"), (int, float)):
                dist = float(item["dist2"]) ** 0.5
            if not isinstance(dist, (int, float)):
                continue
            found.append({
                "name": str(item.get("name") or item.get("type") or "hostile mob"),
                "dist": float(dist),
                "pos": item.get("pos", item.get("position")),
            })
        if found:
            break
    return sorted(found, key=lambda entry: entry["dist"])


def just_hurt(facts: Mapping[str, Any]) -> bool:
    health, before = facts.get("health"), facts.get("health_before")
    return (isinstance(health, (int, float)) and isinstance(before, (int, float))
            and health < before - HURT_MARGIN)


def _ago(facts: Mapping[str, Any]) -> str:
    age = facts.get("vital_age_s")
    return f"{age:.0f} seconds" if isinstance(age, (int, float)) else "a few seconds"


def _inventory(facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("inventory", "inventory_counts"):
        if isinstance(facts.get(key), Mapping):
            return facts[key]
    return None


def dead(facts: Mapping[str, Any]) -> bool:
    health = facts.get("health")
    return isinstance(health, (int, float)) and health <= 0


_DEAD_OPTIONS = {"call_agent": "The Body has died. Hand control back to the Agent."}


def _body_sentence(facts: Mapping[str, Any]) -> str:
    if dead(facts):
        return "The Body has died."
    parts: list[str] = []
    health, food, air = facts.get("health"), facts.get("food"), facts.get("oxygen")
    if isinstance(health, (int, float)):
        parts.append(f"Health is {health:.0f} of 20.")
    if isinstance(food, (int, float)):
        parts.append(f"Hunger is {food:.0f} of 20.")
    if isinstance(air, (int, float)):
        air_before = facts.get("oxygen_before")
        if air >= AIR_FULL:
            parts.append("Air supply is full.")
        elif air <= 0:
            parts.append("Air supply is empty; the Body is under water and is drowning.")
        elif isinstance(air_before, (int, float)) and air > air_before:
            parts.append(
                f"The Body is breathing again: air supply was {air_before:.0f} {_ago(facts)} ago "
                f"and is {air:.0f} of {AIR_FULL} now."
            )
        elif isinstance(air_before, (int, float)):
            parts.append(
                "The head is under water and the Body cannot breathe: air supply was "
                f"{air_before:.0f} {_ago(facts)} ago and is {air:.0f} of {AIR_FULL} now."
            )
        else:
            parts.append(
                "The head is under water and the Body cannot breathe: air supply is "
                f"{air:.0f} of {AIR_FULL} now."
            )
    if just_hurt(facts):
        parts.append(
            f"The Body was just hurt: health fell from {facts['health_before']:.0f} "
            f"to {health:.0f} in the last {_ago(facts)}."
        )
    return " ".join(parts)


def _threat_sentence(facts: Mapping[str, Any]) -> str:
    near = hostiles(facts)
    if near:
        return f"Nearest hostile mob: {near[0]['name']}, {near[0]['dist']:.0f} blocks away."
    return "No hostile mob is in view."


def _position(value: Any) -> str | None:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return "[" + ", ".join(f"{float(part):.1f}" for part in value) + "]"
        except (TypeError, ValueError):
            return None
    return None


def result_sentence(call: str, result: Mapping[str, Any] | None) -> str:
    """Say how the call ended.  Success is only "ended normally": whether the
    intent is met is for Decision to read from the inventory and the intent."""
    result = result if isinstance(result, Mapping) else {}
    if result.get("success") is True:
        return f"{call} has ended normally."
    return f"{call} has ended. It reported failure: {result.get('reason') or 'no reason given'}."


def attempt_lines(history: list[Mapping[str, Any]], earlier: int = 0) -> list[str]:
    """Earlier attempts of the same call, oldest first, the current one left out.

    ``history`` holds the recent attempts including the current one; each has
    ``result`` and the ``body`` (position, inventory) seen when it ended.
    """
    if len(history) < 2 and not earlier:
        return []
    now = history[-1].get("body") if history else None
    lines = [f"There were {earlier} earlier attempts before these."] if earlier else []
    for index, attempt in enumerate(history[:-1], start=earlier + 1):
        result = attempt.get("result") if isinstance(attempt.get("result"), Mapping) else {}
        if result.get("success") is True:
            lines.append(f"Attempt {index}: ended normally.")
            continue
        same = attempt.get("body") == now
        lines.append(
            f"Attempt {index}: failure: {result.get('reason') or 'no reason given'}; "
            + ("inventory and position unchanged." if same else "inventory or position changed since.")
        )
    return lines


def frame_state(
    *, goal: str, intent: str, tool: str, params: Mapping[str, Any] | None,
    facts: Mapping[str, Any], running: bool, phase: str | None = None,
    elapsed_s: float | None = None, progress_signal: str | None = None,
    since_progress_s: float | None = None, result: Mapping[str, Any] | None = None,
    earlier_attempts: list[str] | None = None,
) -> dict[str, Any]:
    """Describe one frame.  Every fact is said once, as a sentence."""
    call = call_text(tool, params)
    state: dict[str, Any] = {"task": goal, "agent_intent": intent}
    inventory = _inventory(facts)
    if inventory is not None:
        state["inventory"] = ", ".join(f"{item} {count}" for item, count in inventory.items()) or "empty"
    state["body"] = _body_sentence(facts)
    state["threats"] = _threat_sentence(facts)
    if running:
        work = f"{call} is running."
        if isinstance(elapsed_s, (int, float)):
            work += f" elapsed {elapsed_s:.1f}s."
        if phase:
            work += f" Phase: {phase}."
        state["current_work"] = work
        if progress_signal and isinstance(since_progress_s, (int, float)):
            progress = f"last progress event: {progress_signal}, {since_progress_s:.1f}s ago."
        else:
            progress = "no real progress has been observed yet."
        delta = facts.get("inventory_delta")
        if isinstance(delta, Mapping) and delta:
            progress += " inventory changed: " + ", ".join(
                f"{item} {amount:+d}" for item, amount in delta.items() if isinstance(amount, int)
            ) + "."
        state["progress"] = progress
    else:
        state["finished_call"] = result_sentence(call, result)
        if earlier_attempts:
            state["earlier_attempts_of_this_call"] = list(earlier_attempts)
    start = _position(facts.get("start_position"))
    now = _position(facts.get("position", facts.get("pos")))
    if start and now:
        state["position"] = f"started at {start}; now at {now}."
    elif now:
        state["position"] = f"now at {now}."
    return {key: value for key, value in state.items() if value not in (None, "")}


def frame_options(
    *, tool: str, params: Mapping[str, Any] | None, intent: str,
    facts: Mapping[str, Any], running: bool, result: Mapping[str, Any] | None = None,
    next_call: str = "", tree: Mapping[str, str] | None = None, repeat_note: str = "",
    intent_of_plan: bool = False,
) -> dict[str, str]:
    """The concrete things the Body may do next in this frame.

    ``intent_of_plan`` says the step wrote no intent of its own and ``intent``
    is the one of the plan around it.
    """
    if dead(facts):
        # A dead Body can do none of the other things.
        return dict(_DEAD_OPTIONS)
    call = call_text(tool, params)
    result = result if isinstance(result, Mapping) else {}
    failed = not running and result.get("success") is not True
    stop = "Stop the current action" if running else "Do not repeat it"
    options: dict[str, str] = {}
    if running:
        if intent and intent_of_plan:
            # Said as the plan's words: read as this step's own purpose, a
            # plan that ends in the next step makes the next step look due.
            options["keep_going"] = f"Keep doing the current step, {call}. It is one step of the plan: “{intent}”"
        elif intent:
            options["keep_going"] = f"Keep doing the current work, {call}, for: “{intent}”"
        else:
            options["keep_going"] = f"Keep doing the current work, {call}."
    elif not (failed and result.get("canRetry") is False):
        options["repeat"] = f"Run the same call again: {call}." + (f" {repeat_note}" if repeat_note else "")
    # After a failed step the plan cannot simply move on: later steps depend on it.
    if not failed:
        options["next_step"] = (
            f"{stop} and start the next step of the plan: {next_call}." if next_call
            else f"{stop} and report to the Agent that this work is finished."
        )
    for key, text in (tree or {}).items():
        options[str(key)] = str(text)
    air = facts.get("oxygen")
    if isinstance(air, (int, float)) and air < AIR_FULL:
        options["surface"] = f"{stop} and swim up to air now."
    food_item, food = facts.get("food_item"), facts.get("food")
    if isinstance(food_item, str) and food_item and isinstance(food, (int, float)) and food < 20:
        options["eat"] = f"{stop} and eat {food_item} from the inventory."
    near = hostiles(facts)
    hurt = just_hurt(facts)
    if near and (near[0]["dist"] <= HOSTILE_REACH or hurt):
        options["move_away"] = f"{stop} and move away from the {near[0]['name']}."
    if hurt and _position(facts.get("safe_standing_position", facts.get("last_safe_position"))):
        options["retreat_safe"] = f"{stop} and go back to the last place where the Body stood safely."
    options["call_agent"] = f"{stop} and hand control back to the Agent for a new plan."
    return options


def after_default_options(
    *, original_call: str, intent: str, default_call: str, result: Mapping[str, Any] | None,
    facts: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """What the Body may do once a protective action has ended."""
    if dead(facts or {}):
        return dict(_DEAD_OPTIONS)
    result = result if isinstance(result, Mapping) else {}
    options = {
        "resume": f"Go back to the earlier work, {original_call}" + (f", for: “{intent}”" if intent else "."),
    }
    if not (result.get("success") is not True and result.get("canRetry") is False):
        options["repeat"] = f"Run the same call again: {default_call}."
    options["call_agent"] = "Do not go back and hand control back to the Agent for a new plan."
    return options


def frame_questions(options: Mapping[str, str]) -> dict[str, Any]:
    """One request.  The danger question is asked only when a protective
    action is on offer in this frame."""
    protective = {key: text for key, text in options.items() if key in PROTECTIVE}
    normal = {key: text for key, text in options.items() if key not in PROTECTIVE}
    questions: dict[str, Any] = {
        "q": {"type": "choice", "instructions": "Choose what the Body does next.", "criteria": normal},
    }
    if protective:
        questions["d"] = {"type": "choice", "instructions": "Is the Body in danger?", "criteria": dict(_DANGER)}
        questions["p"] = {
            "type": "choice", "instructions": "Choose how the Body protects itself.",
            "criteria": {**protective, **({"call_agent": options["call_agent"]} if "call_agent" in options else {})},
        }
    return questions


def idle_options(facts: Mapping[str, Any]) -> dict[str, str]:
    """The Step-2 idle range: waiting plus the applicable protective calls."""
    options = {"stay": "Stay where the Body is and wait for the next step."}
    air = facts.get("oxygen")
    if isinstance(air, (int, float)) and air < AIR_FULL:
        options["surface"] = "Swim up to air."
    food_item, food = facts.get("food_item"), facts.get("food")
    if isinstance(food_item, str) and food_item and isinstance(food, (int, float)) and food < 20:
        options["eat"] = f"Eat {food_item} from the inventory."
    near = hostiles(facts)
    if near and near[0].get("dist", float("inf")) <= HOSTILE_REACH:
        options["move_away"] = "Move away from the nearby hostile entity."
    safe = facts.get("safe_standing_position", facts.get("last_safe_position"))
    if just_hurt(facts) and _position(safe):
        options["retreat_safe"] = "Go back to the last place where the Body stood safely."
    return options


def idle_question(facts: Mapping[str, Any], options: Mapping[str, str]) -> dict[str, Any]:
    """Compile idle prose here, alongside the work-frame prose."""
    state: dict[str, Any] = {"body": _body_sentence(facts)}
    threats = _threat_sentence(facts)
    if threats:
        state["threats"] = threats
    inventory = _inventory(facts)
    if inventory is not None:
        state["inventory"] = ", ".join(f"{k}={v}" for k, v in inventory.items()) or "empty"
    position = _position(facts.get("position", facts.get("pos")))
    if position:
        state["position"] = f"now at {position}."
    return {"state": state, "questions": frame_questions(options)}


def only_option(questions: Mapping[str, Any]) -> str | None:
    """When a frame leaves a single thing to do there is nothing to ask."""
    if set(questions) == {"q"} and len(questions["q"]["criteria"]) == 1:
        return next(iter(questions["q"]["criteria"]))
    return None


def frame_choice(raw: Mapping[str, Any] | None, questions: Mapping[str, Any]) -> str:
    """Read Decision's answer: protective action if in danger, else normal work."""
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    if not isinstance(answers, Mapping):
        return "unknown"

    def chosen(name: str) -> str | None:
        answer = answers.get(name)
        choice = answer.get("choice") if isinstance(answer, Mapping) else None
        allowed = (questions.get(name) or {}).get("criteria") or {}
        return choice if isinstance(choice, str) and choice in allowed else None

    name = "q"
    if "d" in questions:
        danger = chosen("d")
        if danger not in {"safe", "in_danger"}:
            return "unknown"
        if danger == "in_danger":
            name = "p"
    return chosen(name) or "unknown"


__all__ = [
    "PROTECTIVE", "VitalWindow", "after_default_options", "attempt_lines", "call_text", "frame_choice",
    "frame_options", "frame_questions", "frame_state", "hostiles", "just_hurt",
    "idle_options", "idle_question",
    "only_option", "result_sentence",
]
