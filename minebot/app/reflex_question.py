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
PROTECTIVE = ("surface", "engage", "ranged", "move_away", "retreat_safe")
# A hostile mob farther than this cannot be escaped by one short move.
HOSTILE_REACH = 16.0
AIR_FULL = 300
DEFAULT_ACTION_INTENTS = {
    "surface": "Swim up to air.",
    "retreat_safe": "Go back to the last place where the Body stood safely.",
    "eat": "Eat food from the inventory.",
    "move_away": "Move away from the nearby hostile mob.",
    "engage": "Fight back against the selected hostile mob.",
    "ranged": "Shoot the selected hostile mob.",
    "go_home": "Walk to the home position.",
    "approach_player": "Walk to the nearby player.",
    "follow_player": "Follow the nearby player.",
    "look_player": "Look toward the nearby player.",
    "seek_player": "Find the player named in the life profile.",
}
# A health change smaller than this is regeneration noise, not an injury.
HURT_MARGIN = 0.4
# Within this many blocks of a place the Body is at it (the task book's
# "beside the player" / "at home"): an option to go there is not offered.
AT_PLACE_BLOCKS = 2
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
        if "oxygen" in sample:
            # The current uninterrupted direction of air change, rather than
            # a net change across an earlier breath followed by another dive.
            air = sample["oxygen"]
            direction = 0
            baseline = None
            for stamp, values in reversed(self._samples[:-1]):
                if "oxygen" not in values:
                    continue
                previous = values["oxygen"]
                change = (air > previous) - (air < previous)
                if change and direction and change != direction:
                    break
                if change:
                    direction = change
                baseline = (stamp, previous)
                air = previous
            before.pop("oxygen_before", None)
            if baseline is not None:
                before["oxygen_before"] = baseline[1]
                before["oxygen_age_s"] = now - baseline[0]
        return before


def protection_experience(stamp: float, tool: str, params: Mapping[str, Any],
                          result: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
    position = _position(facts.get("position", facts.get("pos")))
    return f"At {when}, {result_sentence(call_text(tool, params), result)} Now at {position or 'an unrecorded position'}."


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
            # Native entities can remain visible during their death animation.
            # Observed zero health is a terminal game fact, not a live target
            # or a judgement about whether the Agent's intention is complete.
            health = item.get("health")
            if isinstance(health, (int, float)) and health <= 0:
                continue
            dist = item.get("dist", item.get("distance"))
            if dist is None and isinstance(item.get("dist2"), (int, float)):
                dist = float(item["dist2"]) ** 0.5
            if not isinstance(dist, (int, float)):
                continue
            found.append({
                "id": item.get("id", item.get("uuid")),
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


def _ago(facts: Mapping[str, Any], key: str = "vital_age_s") -> str:
    age = facts.get(key, facts.get("vital_age_s"))
    if not isinstance(age, (int, float)):
        return "a few seconds"
    rounded = round(age)
    return f"{rounded} second" + ("" if rounded == 1 else "s")


def _inventory(facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("inventory", "inventory_counts"):
        if isinstance(facts.get(key), Mapping):
            return facts[key]
    return None


def dead(facts: Mapping[str, Any]) -> bool:
    health = facts.get("health")
    return isinstance(health, (int, float)) and health <= 0


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
        elif facts.get("head_in_liquid") is False:
            if isinstance(air_before, (int, float)) and air > air_before:
                parts.append(
                    f"The Body is breathing again: air supply was {air_before:.0f} {_ago(facts, 'oxygen_age_s')} ago "
                    f"and is {air:.0f} of {AIR_FULL} now."
                )
            else:
                parts.append(f"The head is out of liquid; air supply is {air:.0f} of {AIR_FULL} now.")
        elif air <= 0:
            parts.append("Air supply is empty; the Body is under water and is drowning.")
        elif facts.get("head_in_liquid") is not True and isinstance(air_before, (int, float)) and air > air_before:
            parts.append(
                f"The Body is breathing again: air supply was {air_before:.0f} {_ago(facts, 'oxygen_age_s')} ago "
                f"and is {air:.0f} of {AIR_FULL} now."
            )
        elif isinstance(air_before, (int, float)):
            parts.append(
                "The head is under water and the Body cannot breathe: air supply was "
                f"{air_before:.0f} {_ago(facts, 'oxygen_age_s')} ago and is {air:.0f} of {AIR_FULL} now."
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


def _main_hand(facts: Mapping[str, Any]) -> str | None:
    """The observed main-hand item, said beside the inventory.  Said inside
    the body sentence it was overlooked when an intent named both an item and
    holding it.  An unobserved hand is left out, never written as empty."""
    selected = facts.get("selected_item")
    if dead(facts) or facts.get("selected_item_known") is False or not isinstance(selected, str):
        return None
    return selected or "nothing"


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
    sentence = f"{call} has ended. It reported failure: {result.get('reason') or 'no reason given'}."
    # The native perception result already records its scope and observation
    # count. Preserve that fact after the Agent projector moves it to summary;
    # it does not establish that the target is absent from the whole world.
    for field in ("summary", "metrics"):
        details = result.get(field)
        if not isinstance(details, Mapping):
            continue
        count, radius = details.get("entities_observed"), details.get("search_radius")
        if type(count) is int and type(radius) in (int, float):
            return sentence + f" The tool observed {count} entities within {radius:g} blocks."
    return sentence


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
        interruption = _interruption(result)
        if interruption:
            lines.extend(interruption_lines([attempt], earlier=index - 1, include_count=False))
            continue
        if result.get("success") is True:
            lines.append(f"Attempt {index}: ended normally.")
            continue
        same = attempt.get("body") == now
        lines.append(
            f"Attempt {index}: failure: {result.get('reason') or 'no reason given'}; "
            + ("inventory and position unchanged." if same else "inventory or position changed since.")
        )
    return lines


def _interruption(result: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = result.get("metrics")
    handoff = metrics.get("direct_call_handoff") if isinstance(metrics, Mapping) else None
    if not isinstance(handoff, Mapping):
        handoff = result.get("supervision")
    facts = handoff.get("facts") if isinstance(handoff, Mapping) else None
    return facts if isinstance(facts, Mapping) and facts.get("default_action") else {}


def interruption_lines(history: list[Mapping[str, Any]], earlier: int = 0, *,
                       style: str = "attempts", include_count: bool = True) -> list[str]:
    """All attempts of the suspended work, including the one just stopped.

    Its current attempt is distinct from the protective call's current result.
    No judgement about whether another attempt is worthwhile is added.
    The alternate styles are for the required offline wording comparison.
    """
    lines = [f"There were {earlier} earlier attempts before these."] if earlier and include_count else []
    for index, attempt in enumerate(history, start=earlier + 1):
        result = attempt.get("result") if isinstance(attempt.get("result"), Mapping) else {}
        facts = _interruption(result)
        default = facts.get("default_action") or {}
        tool = default.get("tool")
        if not tool:
            lines.append(f"Attempt {index}: {result_sentence('the original call', result)}")
            continue
        body = attempt.get("body") or {}
        position = _position(body.get("position", body.get("pos")))
        inventory = _inventory(body)
        values = (", ".join(f"{k} {v}" for k, v in inventory.items()) or "empty") if inventory is not None else "not recorded"
        protective_result = default.get("result")
        protective_result = protective_result if isinstance(protective_result, Mapping) else {}
        reason = result.get("reason") or "no reason given"
        if style == "events":
            prefix = f"On attempt {index}, Decision stopped the original work ({reason}) and the Body ran {tool}()."
        elif style == "outcomes":
            prefix = f"Attempt {index} of the original work returned {reason}; the Body switched to {tool}()."
        else:
            prefix = f"Attempt {index}: the original work was interrupted for {tool}() ({reason})."
        if protective_result:
            prefix += " " + result_sentence(f"{tool}()", protective_result)
        lines.append(prefix + f" Inventory at interruption: {values}. Position: {position or 'not recorded'}.")
    return lines


def frame_state(
    *, goal: str, intent: str, tool: str, params: Mapping[str, Any] | None,
    facts: Mapping[str, Any], running: bool, phase: str | None = None,
    elapsed_s: float | None = None, progress_signal: str | None = None,
    since_progress_s: float | None = None, result: Mapping[str, Any] | None = None,
    earlier_attempts: list[str] | None = None,
    interrupted_attempts: list[str] | None = None,
) -> dict[str, Any]:
    """Describe one frame.  Every fact is said once, as a sentence."""
    call = call_text(tool, params)
    state: dict[str, Any] = {
        "task": goal,
        # Outcome-shaped Agent words such as "there is a pickaxe in the
        # inventory" are an intention, not an observed inventory fact.
        # This clarification was verified on recorded running frames; keep
        # the separately verified terminal-frame wording unchanged.
        "agent_intent": f"The Agent wants the following result: “{intent}”." if running and intent else intent,
    }
    inventory = _inventory(facts)
    if inventory is not None:
        state["inventory"] = ", ".join(f"{item} {count}" for item, count in inventory.items()) or "empty"
    state["main_hand"] = _main_hand(facts)
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
        if interrupted_attempts:
            state["interruptions_of_the_original_work"] = list(interrupted_attempts)
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
    interrupt: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """The concrete things the Body may do next in this frame.

    ``intent_of_plan`` says the step wrote no intent of its own and ``intent``
    is the one of the plan around it.
    """
    if dead(facts):
        return {}
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
    # While a call runs for an intent, whether the facts already show it is
    # asked as its own question (frame_questions); the next step follows from
    # that answer, not from an option beside "keep going".
    if not failed and not (running and intent):
        options["next_step"] = (
            f"{stop} and start the next step of the plan: {next_call}." if next_call
            else f"{stop} and report to the Agent that this work is finished."
        )
    for key, text in (tree or {}).items():
        options[str(key)] = str(text)
    if running and isinstance(interrupt, Mapping):
        condition = interrupt.get("condition")
        handler = interrupt.get("handler")
        if isinstance(condition, Mapping) and isinstance(handler, Mapping):
            options["interrupt"] = (
                f"Stop the current action because “{condition.get('text', '')}” "
                f"and do: {handler.get('intent', '')}."
            )
    if failed:
        options.update(equipment_options(facts, result, call=call))
    options.update(protective_options(facts, stop=stop))
    options["call_agent"] = f"{stop} and hand control back to the Agent for a new plan."
    return options


def equipment_candidates(facts: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, str]:
    """Other owned tools of the observed held/broken tool's game category."""
    held = str(facts.get("selected_item") or "").removeprefix("minecraft:")
    source = held
    for details in (result.get("summary"), result.get("metrics")):
        if isinstance(details, Mapping) and isinstance(details.get("broken_item"), str):
            source = details["broken_item"].removeprefix("minecraft:")
            break
    category = next((kind for kind in ("pickaxe", "axe", "shovel", "sword")
                     if source.endswith("_" + kind)), None)
    if category is None:
        return {}
    return {"equip:" + str(item).removeprefix("minecraft:"): str(item)
            for item, count in sorted((_inventory(facts) or {}).items())
            if isinstance(count, (int, float)) and count > 0
            and str(item).endswith("_" + category)
            and str(item).removeprefix("minecraft:") not in {held, source}}


# Result reasons in which the Body reports that its tool was the problem.
TOOL_FAILURE_REASONS = ("tool_broke", "missing_required_tool")


def tool_failed(result: Mapping[str, Any]) -> bool:
    """The result itself says the tool broke or was not the one needed."""
    details = [result] + [d for d in (result.get("summary"), result.get("metrics")) if isinstance(d, Mapping)]
    if any(isinstance(d.get("broken_item"), str) for d in details):
        return True
    reasons = [d.get("reason") for d in details if isinstance(d.get("reason"), str)]
    return any(r in TOOL_FAILURE_REASONS or r.startswith("tool_equip_failed") for r in reasons)


def equipment_options(facts: Mapping[str, Any], result: Mapping[str, Any], *, call: str) -> dict[str, str]:
    """Owned tools of the same category, when a call failed because of its tool.

    Offered for any failure, "equip the stone sword and run the same call
    again" was chosen after a fight ended with its target gone and the sword
    intact, 2 of 2 times (live step8-extra-claude3), and after a failed move
    away (step8-extra-claude1). The user chose tool failures only (2026-10-15).
    """
    if result.get("success") is True or not tool_failed(result):
        return {}
    return {key: f"Equip {item} from the inventory and run the same call again: {call}."
            for key, item in equipment_candidates(facts, result).items()}


def equipment_intent(item: str) -> str:
    return f"Hold {item} in the main hand."


def repeat_note(again_when: str, until: str) -> str:
    """The Agent's own repeat conditions, said beside the same-call option."""
    return f"Repeat when: {again_when}; stop when: {until}."


def protective_options(facts: Mapping[str, Any], *, stop: str = "") -> dict[str, str]:
    """One eligibility compiler for work and idle protective tools."""
    options: dict[str, str] = {}
    prefix = f"{stop} and " if stop else ""
    def text(action: str) -> str:
        return prefix + action if prefix else action[0].upper() + action[1:]
    air = facts.get("oxygen")
    if isinstance(air, (int, float)) and air < AIR_FULL:
        options["surface"] = text("swim up to air now.")
    food_item, food = facts.get("food_item"), facts.get("food")
    if isinstance(food_item, str) and food_item and isinstance(food, (int, float)) and food < 20:
        options["eat"] = text(f"eat {food_item} from the inventory.")
    near = hostiles(facts)
    hurt = just_hurt(facts)
    if near and (near[0]["dist"] <= HOSTILE_REACH or hurt):
        if isinstance(near[0].get("id"), str) and near[0]["id"]:
            # Said in the option: with only "in melee", a zombie at one block
            # and an iron sword in hand still lost to fleeing (probe 24/36 vs 36/36).
            hand = _main_hand(facts)
            held_with = f" with the {hand} in the main hand" if hand and hand != "nothing" else ""
            options["engage"] = text(f"fight back against the {near[0]['name']} in melee{held_with}.")
            inventory = _inventory(facts) or {}
            held = {str(item).removeprefix("minecraft:"): count for item, count in inventory.items()}
            weapon = next((weapon for weapon in ("bow", "crossbow") if held.get(weapon, 0) > 0), None)
            if weapon and held.get("arrow", 0) > 0:
                options["ranged"] = text(f"shoot the {near[0]['name']} with the {weapon} from the inventory.")
        options["move_away"] = text(f"move away from the {near[0]['name']}.")
    safe = facts.get("safe_standing_position", facts.get("last_safe_position"))
    away = player_distance({"pos": safe}, facts) if _position(safe) else None
    # Standing there already, going back is no move at all: chosen again and
    # again, it arrived at once 11 times in 7 s (step5-threat-claude8).
    if hurt and _position(safe) and (away is None or away > AT_PLACE_BLOCKS):
        options["retreat_safe"] = text("go back to the last place where the Body stood safely.")
    return options


# What a running protective action is judged on.
PROTECTION_FACTS = ("body", "threats", "inventory", "main_hand", "position")


def protecting_options(facts: Mapping[str, Any], current: str) -> dict[str, str]:
    """While a protective action runs, every frame asks the protective choice
    again; the running action is one of the options, not a "keep going".
    Offered "keep going", Decision kept fleeing at one block while health fell
    from 18 to 2 (live step5-threat-claude2)."""
    options = protective_options(facts)
    if current not in options:
        options = {current: DEFAULT_ACTION_INTENTS.get(current, current), **options}
    options["call_agent"] = "Hand control back to the Agent for a new plan."
    return options


def after_default_options(
    *, original_call: str, intent: str, default_call: str, result: Mapping[str, Any] | None,
    facts: Mapping[str, Any] | None = None, protective_choice: str = "",
) -> dict[str, str]:
    """What the Body may do once a protective action has ended: go back (or
    stay), any protective action the facts allow now, owned spare equipment,
    or the Agent.

    Offered as "Run the same call again", a fight refused at low health was
    repeated twelve times under a skeleton's arrows (step5-threat-claude4);
    named beside the others, the replay chose to move away 18/18.  The one
    that just ended follows the same eligibility as the others: offered
    anyway, going back to the safe place where the Body already stood was
    chosen 12 times in a row (step5-threat-claude9)."""
    if dead(facts or {}):
        return {}
    result = result if isinstance(result, Mapping) else {}
    options = {
        "resume": f"Go back to the earlier work, {original_call}" + (f", for: “{intent}”" if intent else "."),
    } if original_call else {"stay": idle_options({})["stay"]}
    options.update(protective_options(facts or {}))
    options.update(equipment_options(facts or {}, result, call=default_call))
    options["call_agent"] = "Do not go back and hand control back to the Agent for a new plan."
    return options


def frame_questions(
    options: Mapping[str, str], *, protecting: bool = False, acting_for_intent: bool = False,
    interrupt: Mapping[str, Any] | None = None, running_protection: bool = False,
) -> dict[str, Any]:
    """One request.  The danger question is asked only when a protective
    action is on offer in this frame.  Whether running work has reached its
    intent is asked in its own request (done_question)."""
    protective = {key: text for key, text in options.items() if key in PROTECTIVE and not protecting}
    normal = dict(options) if protecting else {key: text for key, text in options.items() if key not in PROTECTIVE}
    questions: dict[str, Any] = {
        "q": {"type": "choice", "instructions": (
            "Choose how the Body protects itself right now." if running_protection
            else "Choose what the Body does right now to achieve the Agent's intent."
            if acting_for_intent else "Choose what the Body does next."
        ), "criteria": normal},
    }
    if "interrupt" in normal and isinstance(interrupt, Mapping):
        condition, handler = interrupt.get("condition"), interrupt.get("handler")
        if isinstance(condition, Mapping) and isinstance(handler, Mapping):
            questions["q"]["instructions"] += (
                f" The Agent also says: when “{condition.get('text', '')}”, "
                f"interrupt the current work and do “{handler.get('intent', '')}”.")
    if protective:
        questions["d"] = {"type": "choice", "instructions": "Is the Body in danger?", "criteria": dict(_DANGER)}
        questions["p"] = {
            "type": "choice", "instructions": "Choose how the Body protects itself.",
            "criteria": {**protective, **({"call_agent": options["call_agent"]} if "call_agent" in options else {})},
        }
    return questions


def interrupt_result_question(*, goal: str, intent: str, original_call: str,
                              handler_intent: str, handler_call: str,
                              facts: Mapping[str, Any]) -> dict[str, Any]:
    state = frame_state(goal=goal, intent=intent, tool="interrupt_handler", params={},
                        facts=facts, running=False,
                        result={"success": True, "reason": "handler_completed"})
    state["finished_call"] = f"The interruption handler for “{handler_intent}” has ended normally."
    options = after_default_options(
        original_call=original_call, default_call=handler_call, intent=intent,
        result={"success": True}, facts=facts)
    options["repeat"] = f"Do the interruption handler again: “{handler_intent}”, starting with {handler_call}."
    return {"state": state, "questions": frame_questions(options)}


def nearby_players(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    # Native action frames and perception reads use different names for the
    # same bounded entity sample. An explicit empty sample is authoritative.
    entities = next((facts[key] for key in ("nearby_entities", "entities", "entities_near")
                     if isinstance(facts.get(key), (list, tuple))), [])
    return [item for item in entities
            if isinstance(item, Mapping)
            and str(item.get("entity_type", item.get("type", ""))).removeprefix("minecraft:") == "player"]


def player_distance(player: Mapping[str, Any], facts: Mapping[str, Any]) -> float | None:
    if isinstance(player.get("dist"), (int, float)):
        return float(player["dist"])
    if isinstance(player.get("dist2"), (int, float)):
        return max(0, player["dist2"]) ** 0.5
    here, there = facts.get("position", facts.get("pos")), player.get("pos")
    if isinstance(here, (list, tuple)) and isinstance(there, (list, tuple)) and len(here) == len(there) == 3:
        return sum((float(a) - float(b)) ** 2 for a, b in zip(here, there)) ** 0.5
    return None


def life_running_options(facts: Mapping[str, Any], running: str) -> dict[str, str]:
    """A running life action: the same idle range with the running action under
    its own name, first.  Offered only as "Keep doing the current life activity:
    __reflex_look_at(...)", it was never chosen: following a walking player
    switched between follow, approach and look every frame (step8-extra-claude2;
    offline 64/64 switches, named 60/64 carried on)."""
    options = idle_options(facts)
    if running in options:
        return {running: options[running], **{key: text for key, text in options.items() if key != running}}
    return options


def idle_options(facts: Mapping[str, Any]) -> dict[str, str]:
    """Idle range compiled from generic player/profile facts and protection."""
    if dead(facts):
        return {}
    options: dict[str, str] = {}
    profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
    players = nearby_players(facts) if profile else []
    thinking = facts.get("agent_thinking") is True
    if players:
        player = players[0]
        name = str(player.get("name") or player.get("id") or "the nearby player")
        options["look_player"] = f"Look toward {name} at the observed position."
        distance = player_distance(player, facts)
        # This is the existing approach tool's arrival band, not a judgement
        # about when the life intention has been achieved.
        if distance is not None and distance > 2:
            options["approach_player"] = f"Walk to {name} at the observed position."
        if not thinking:
            options["follow_player"] = f"Follow {name} as a moving player."
    home = profile.get("home") if isinstance(profile, Mapping) else None
    position = _position(facts.get("position", facts.get("pos")))
    home_pos = _position(home.get("pos")) if isinstance(home, Mapping) else None
    at_home = False
    if home_pos and position:
        at_home = player_distance({"pos": home.get("pos")}, facts) <= AT_PLACE_BLOCKS
    if home_pos and position and not at_home and not thinking:
        options["go_home"] = f"Walk to the home position {home_pos}."
    if not thinking:
        relationships = profile.get("relationships") or []
        visible = {str(p.get("name")) for p in players}
        for relationship in relationships:
            if isinstance(relationship, Mapping) and relationship.get("player") not in visible:
                name = str(relationship.get("player") or "")
                if name:
                    options.setdefault("seek_player", f"Search nearby for {name} and walk up to them.")
        # The Agent's own projects: write work, so never while it thinks.
        # Wording from bench-project2 (step7-life-claude2 frames, two routine
        # wordings, 12 orders): "Leave what I am doing ... in my area" was
        # chosen 72/72 by day and 0/72 at dusk at home; "Work on my project"
        # 26/72 and "Go to my area" 62/72 at dusk.
        for project in profile.get("projects") or []:
            if isinstance(project, Mapping) and project.get("status") == "active" and project.get("id"):
                options[f"project_{project['id']}"] = (
                    f"Leave what I am doing and work on my project “{project['id']}” "
                    f"in my area “{project.get('area', '')}”: {project.get('intent', '')}")
    options["stay"] = "Stay where the Body is and wait for the next step."
    options.update(protective_options(facts))
    # While the Agent is thinking it is already awake; calling it adds nothing.
    if profile and not thinking:
        options["call_agent"] = "Hand control back to the Agent for a new direction."
    return options


def life_surroundings(facts: Mapping[str, Any]) -> dict[str, str]:
    """Daylight and nearby players, said the same way in idle and project frames."""
    state: dict[str, str] = {}
    clock = facts.get("time")
    if isinstance(clock, (int, float)):
        tick = clock % 24000
        state["daylight"] = ("It is day." if tick < 12000 or tick >= 23000 else
                             "It is dusk; night falls soon." if tick < 13000 else "It is night.")
    players = nearby_players(facts)
    if players:
        lines = []
        for player in players:
            distance = player_distance(player, facts)
            name = player.get("name") or player.get("id") or "a nearby player"
            lines.append(f"Player {name} is {distance:.0f} blocks away." if distance is not None
                         else f"Player {name} is in view.")
        state["players"] = " ".join(lines)
    return state


def project_frame(state: Mapping[str, Any], options: Mapping[str, str],
                  facts: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """A running frame of the Agent's own project: the work frame, plus the
    life intention, daylight and players, and the option to put it down."""
    shown = dict(state)
    if facts.get("life_intent"):
        shown["life_intent"] = f"This is the idle-life intention: “{facts['life_intent']}”."
    shown.update(life_surroundings(facts))
    put_down = "Stop the current action and put the project down for now; go back to everyday life."
    if "call_agent" not in options:
        return shown, {**options, "put_down": put_down}
    ordered = {key: text for key, text in options.items() if key != "call_agent"}
    return shown, {**ordered, "put_down": put_down, "call_agent": options["call_agent"]}


def idle_question(facts: Mapping[str, Any], options: Mapping[str, str]) -> dict[str, Any]:
    """Compile idle prose here, alongside the work-frame prose."""
    state: dict[str, Any] = {"body": _body_sentence(facts)}
    if facts.get("life_intent"):
        state["agent_intent"] = f"This is the idle-life intention: “{facts['life_intent']}”."
    state.update(life_surroundings(facts))
    threats = _threat_sentence(facts)
    if threats:
        state["threats"] = threats
    inventory = _inventory(facts)
    if inventory is not None:
        state["inventory"] = ", ".join(f"{k} {v}" for k, v in inventory.items()) or "empty"
    hand = _main_hand(facts)
    if hand:
        state["main_hand"] = hand
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


def in_danger(raw: Mapping[str, Any] | None, questions: Mapping[str, Any]) -> bool:
    """Whether this answer took the protective branch."""
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    answer = answers.get("d") if isinstance(answers, Mapping) else None
    return "d" in questions and isinstance(answer, Mapping) and answer.get("choice") == "in_danger"


# Facts that can show an intent.  The 4B single-pass Decision compares an
# intent with them correctly only when nothing else is in view: the progress
# line ("inventory changed: oak_log +3") reads as "done" to it, and so do the
# task and the call text (docs/references/reflex-always-on-20261012/
# decision-capability-20261014.md).
_DONE_FACTS = ("inventory", "main_hand", "position")


def done_question(state: Mapping[str, Any], intent: str) -> dict[str, Any]:
    """Whether running work has reached its intent, asked in its own request
    with only the facts the intent can be checked against."""
    small: dict[str, Any] = {"agent_intent": f"The Agent wants the following result: “{intent}”."}
    small.update({key: state[key] for key in _DONE_FACTS if state.get(key)})
    return {"state": small, "questions": {"a": {"type": "choice", "instructions": (
        "Compare each part of the result the Agent wants with the inventory and Body facts above. "
        "Do the facts already show every part of it?"
    ), "criteria": {
        "shown": f"Yes: the facts above already show “{intent}”.",
        "not_shown": f"No: the facts above do not show all of “{intent}” yet.",
    }}}}


def return_place_question(*, work_call: str, intent: str, before: Any, now: Any) -> dict[str, Any] | None:
    """Whether danger stopped the work again where it stopped it before the
    Body last went back.  The 4B Decision compares two places well but not
    a count of interruptions; whether to risk going back again is then the
    Agent's (decision-capability-20261014.md)."""
    before_at, now_at = _position(before), _position(now)
    if not before_at or not now_at:
        return None
    return {"state": {
        "agent_intent": f"The earlier work was for: “{intent}”." if intent else f"The earlier work was {work_call}.",
        "last_return": f"Before the Body last went back to {work_call}, danger stopped it at {before_at}.",
        "this_interruption": f"Now danger has stopped it again at {now_at}.",
    }, "questions": {"r": {"type": "choice", "instructions": (
        "Compare the two places. Did danger stop the work again at about the same place?"
    ), "criteria": {
        "same": "Yes: about the same place as before the last return.",
        "different": "No: somewhere else.",
    }}}}


def return_place_choice(raw: Mapping[str, Any] | None) -> str | None:
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    answer = answers.get("r") if isinstance(answers, Mapping) else None
    choice = answer.get("choice") if isinstance(answer, Mapping) else None
    return choice if choice in {"same", "different"} else None


def done_choice(raw: Mapping[str, Any] | None) -> str | None:
    """"shown" / "not_shown", or None when there is no valid answer."""
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    answer = answers.get("a") if isinstance(answers, Mapping) else None
    choice = answer.get("choice") if isinstance(answer, Mapping) else None
    return choice if choice in {"shown", "not_shown"} else None


__all__ = [
    "PROTECTIVE", "VitalWindow", "after_default_options", "attempt_lines", "call_text", "frame_choice",
    "protecting_options", "PROTECTION_FACTS",
    "frame_options", "frame_questions", "frame_state", "hostiles", "just_hurt",
    "idle_options", "idle_question", "done_question", "done_choice", "in_danger",
    "return_place_question", "return_place_choice",
    "only_option", "result_sentence",
]
