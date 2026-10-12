"""Default Decision supervision for every mutating Body call.

The supervisor compiles bounded facts from :mod:`execution_snapshot`, asks one
finite-choice Decision question for each new fact frame, and leaves admission,
stopping and settlement to the existing runtime entry points. Direct calls and
plan call nodes use this same path.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from uuid import uuid4
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from minebot.app.decision_client import DecisionClient, DecisionPriority
from minebot.app.execution_snapshot import FACT_FRAME_INTERVAL_S
from minebot.contract.execution import tool_observation_scope

MAX_SUPERVISION_READS = 4
# Kept as a compatibility export. R2 no longer caps questions per call.
MAX_SUPERVISION_QUESTIONS = 0
MAX_IDLE_UNKNOWN_FRAMES = 3
MAX_QUESTION_CHARS = 16_000
FAST_COMPLETION_GRACE_S = 0.02
# ``move_away`` is a short-range safety move.  The option is useful only when
# the recorded hazard is close enough that the tool's bounded candidate search
# can change the immediate situation; a distant observation is context, not an
# applicable escape action.
MOVE_AWAY_APPLICABLE_DISTANCE = 16.0

DEFAULT_TOOL_EXPLANATION = (
    "The call normally advances through native Body evidence and eventually "
    "returns one authoritative terminal result. Navigation, phase changes, "
    "world edits and inventory changes are facts; process liveness and a "
    "repeated sample are not."
)

# These are semantic escape hatches, not scene detectors.  Their availability
# comes only from generic facts already present in the execution snapshot.
_DEFAULT_ACTION_TEXT = {
    "go_to_surface": "Move upward to a verified natural surface now, leaving the current underwater or below-grade state.",
    "retreat_safe": "Return to the most recent safe standing position recorded by the Body.",
    "eat": "Consume one available food item and restore the Body's hunger state.",
    "move_away": "Move away from the nearest hostile presence using the recorded hazard position.",
}


def format_call(tool: str, params: Mapping[str, Any] | None = None) -> str:
    """Render a bounded concrete Body call for a Decision option."""
    args = json.dumps(dict(params or {}), ensure_ascii=False, separators=(",", ":"))
    return f"{tool}({args})"


def compile_work_options(
    *,
    tool: str,
    params: Mapping[str, Any] | None = None,
    intent: str = "",
    next_call: str = "",
    running: bool = True,
    result: Mapping[str, Any] | None = None,
    include_inspect: bool = False,
    repeat_text: str | None = None,
) -> dict[str, str]:
    """Compile one concrete option vocabulary used by direct and plan calls."""
    call = format_call(tool, params)
    intent_text = f' toward “{intent}”' if intent else ""
    options: dict[str, str] = {}
    if running:
        options["keep_going"] = f"Continue {call}{intent_text}."
        options["next_step"] = (
            f"Stop {call} and start the next step by calling {next_call}."
            if next_call else f"Stop {call} and report this step finished to the Agent."
        )
        options["call_agent"] = f"Stop {call} and hand control back to the Agent for a new plan."
    else:
        success = isinstance(result, Mapping) and result.get("success") is True
        can_retry = not (isinstance(result, Mapping) and result.get("canRetry") is False)
        if success or can_retry:
            options["repeat"] = f"Run {call} again."
        if success:
            options["next_step"] = (
                f"Treat the result as complete and start the next step by calling {next_call}."
                if next_call else f"Treat the result as complete and report this step finished to the Agent."
            )
        options["call_agent"] = f"Hand control back to the Agent after {call}."
    if repeat_text and "repeat" in options:
        options["repeat"] += f" {repeat_text}"
    if include_inspect:
        options["inspect"] = "Read one new registered fact."
    return options


class CallSupervisor:
    """Observe all mutating calls through one frame-by-frame Decision loop."""

    def __init__(
        self,
        runtime: Any,
        *,
        decision_url: str | None = None,
        # Deprecated aliases retained for callers configuring the old slice.
        suspicion_s: float = 0.0,
        review_interval_s: float = 0.0,
        max_reads: int = MAX_SUPERVISION_READS,
        max_questions: int = MAX_SUPERVISION_QUESTIONS,
        idle_unknown_limit: int = MAX_IDLE_UNKNOWN_FRAMES,
    ) -> None:
        self.runtime = runtime
        self.decision_url = decision_url or os.environ.get(
            "MINEBOT_DECISION_URL", "http://117.176.14.165:10004/v1/systemone"
        )
        # The old no-progress gate and 15-second review interval are not used.
        self.suspicion_s = suspicion_s
        self.review_interval_s = review_interval_s
        self.max_reads = max(0, int(max_reads))
        self.max_questions = max_questions
        self.idle_unknown_limit = max(1, int(idle_unknown_limit))
        self._client = getattr(runtime, "decision_client", None) or DecisionClient(
            url=self.decision_url, emit=runtime.trace.emit
        )
        self.current: dict[str, Any] | None = None
        self._vital_history: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _with_vital_history(self, call_id: str, facts: Mapping[str, Any]) -> dict[str, Any]:
        current = dict(facts)
        previous = self._vital_history.get(call_id, {})
        for key in ("oxygen", "health"):
            if key in previous and key in current:
                current[f"{key}_previous"] = previous[key]
                if key == "health" and isinstance(previous[key], (int, float)) and isinstance(current[key], (int, float)) and current[key] < previous[key]:
                    current["recently_hurt"] = True
        self._vital_history[call_id] = {key: current[key] for key in ("oxygen", "health") if key in current}
        return current

    @staticmethod
    def _explanation(tool: Any) -> str:
        value = getattr(tool, "supervision_explanation", None)
        if isinstance(value, str) and value.strip():
            return value.strip()[:220]
        description = getattr(tool, "description", None)
        return str(description).strip()[:220] if description else DEFAULT_TOOL_EXPLANATION

    def _snapshot(self, call_id: str) -> dict[str, Any]:
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        current = snapshots.current() if snapshots is not None else None
        if isinstance(current, dict) and current.get("operation_id") == call_id:
            return current
        last = snapshots.last() if snapshots is not None else None
        if isinstance(last, dict) and last.get("operation_id") == call_id:
            return last
        return {}

    @staticmethod
    def _bounded(value: Any, *, depth: int = 0) -> Any:
        if depth >= 5:
            return "<bounded>"
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 32:
                    out["_omitted"] = len(value) - 32
                    break
                out[str(key)[:100]] = CallSupervisor._bounded(item, depth=depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            result = [CallSupervisor._bounded(item, depth=depth + 1) for item in value[:64]]
            if len(value) > 64:
                result.append(f"<{len(value) - 64} omitted>")
            return result
        if isinstance(value, str):
            return value[:500]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:500]

    def _durable_plan_context(self) -> dict[str, Any]:
        """Read the current Agent goal and durable ``in_progress`` step."""
        context = getattr(self.runtime, "agent_context", None)
        goal = str(getattr(context, "goal_text", "") or "")[:800]
        # The durable task plan is a planning aid, not the dispatch intent for
        # a physical call.  Its current_step can lag behind the call by an
        # entire Agent turn, so do not present that stale field to Decision.
        current_step = ""
        workspace = getattr(self.runtime, "native_task_workspace", None)
        try:
            task = None if workspace is None else workspace.current_task
            if task is not None:
                goal = str(getattr(task, "goal_text", "") or goal)[:800]
                plan = workspace.store.get_plan(task.task_id)
                steps = [] if plan is None else getattr(plan, "steps", [])
                for step in steps:
                    status = getattr(getattr(step, "status", None), "value", getattr(step, "status", ""))
                    if str(status) == "in_progress":
                        # Keep the lookup for compatibility with older callers,
                        # but never copy the durable title into a call question.
                        break
        except Exception:
            # Missing plan state is an empty fact, not a reason to make a stop.
            pass
        native_plan = getattr(self.runtime, "native_plan", None)
        if not current_step and isinstance(native_plan, Mapping):
            for step in native_plan.get("steps", []):
                if isinstance(step, Mapping) and str(step.get("status") or "") == "in_progress":
                    break
        return {"goal": goal, "current_step": current_step}

    def _plan_context(
        self, *, supervision_context: Mapping[str, Any] | None,
        tool: Any, params: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan = self._durable_plan_context()
        if isinstance(supervision_context, Mapping):
            for key in ("goal", "node_description", "intent", "intent_chain", "is_plan"):
                if key in supervision_context:
                    plan[key] = supervision_context[key]
        agent_intent = str(plan.get("intent") or "").strip()
        node_description = str(plan.get("node_description") or "").strip()
        if not node_description:
            node_description = agent_intent or str(plan.get("current_step") or "").strip()
        intents = plan.get("intent_chain")
        if not isinstance(intents, list):
            intents = [] if not intents else [str(intents)]
        return self._bounded({
            "goal": str(plan.get("goal") or "")[:180],
            "node_description": node_description[:200],
            "intent": str(plan.get("intent") or "")[:500],
            "intent_chain": [str(item)[:160] for item in intents[:4]],
        })

    @staticmethod
    def _criteria(
        plan: Mapping[str, Any], options: Mapping[str, str] | None = None,
        *, tool_name: str = "the current tool", normal_work: str = "",
    ) -> dict[str, str]:
        if options is not None:
            return {str(key): str(value) for key, value in options.items()}
        intent = str(plan.get("intent") or plan.get("node_description") or "")
        return compile_work_options(tool=tool_name, intent=intent, running=True)

    @staticmethod
    def _compact_execution(execution: Mapping[str, Any]) -> dict[str, Any]:
        """Compile the small set of Body facts Decision needs each frame."""
        facts = execution.get("facts") if isinstance(execution.get("facts"), Mapping) else {}
        keep = (
            "start_position", "position", "navigation", "pathing", "processes",
            "inventory_delta", "inventory", "inventory_counts", "construction", "goal", "failed_path_calculations",
            "builder_paused", "selected_item", "health", "food", "oxygen",
            "in_water", "submerged", "water", "safe_standing_position",
            "last_safe_position", "food_available", "food_item",
            "nearest_hostile_position", "nearby_entities", "nearby_hostiles",
            "entities", "hostiles",
            "required_count", "target_count", "quantity_item", "item", "quantity",
        )
        compact_facts = {key: facts[key] for key in keep if key in facts}
        reads = facts.get("bounded_reads")
        if isinstance(reads, list) and reads:
            compact_facts["bounded_reads"] = reads[-4:]
        facts: dict[str, Any] = {}
        tool = str(execution.get("tool") or "the current tool")
        phase = str(execution.get("phase") or "working")
        params = execution.get("params") if isinstance(execution.get("params"), Mapping) else {}
        facts["doing"] = f"The Body is carrying out {format_call(tool, params)} in the {phase} phase."
        position = compact_facts.get("position")
        start = compact_facts.get("start_position")
        def position_text(value: Any) -> str:
            if isinstance(value, (list, tuple)) and len(value) == 3:
                return "(" + ", ".join(str(v) for v in value) + ")"
            return str(value)
        if start is not None and position is not None:
            facts["where"] = f"The call started at {position_text(start)} and the Body is now at {position_text(position)}."
        elif position is not None:
            facts["where"] = f"The Body is now at {position_text(position)}."
        elif start is not None:
            facts["where"] = f"The call started at {position_text(start)}."
        progress = execution.get("progress_signal")
        age = execution.get("since_progress_s")
        if progress and isinstance(age, (int, float)):
            facts["last_progress"] = f"The last real progress was {progress}, {age:g} seconds ago."
        elif progress:
            facts["last_progress"] = f"The last real progress was {progress}."
        else:
            elapsed = execution.get("elapsed_s")
            if isinstance(elapsed, (int, float)) and elapsed > 0:
                facts["last_progress"] = f"No real progress has been recorded yet; {elapsed:g}s have elapsed since the call began."
            else:
                facts["last_progress"] = "No real progress has been recorded yet."
        delta = compact_facts.get("inventory_delta")
        if isinstance(delta, Mapping) and delta:
            changes = ", ".join(f"{item} {'+' if amount >= 0 else ''}{amount}" for item, amount in delta.items())
            facts["inventory_change"] = f"inventory changed: {changes}."
        inventory = compact_facts.get("inventory")
        if not isinstance(inventory, Mapping):
            inventory = compact_facts.get("inventory_counts")
        if isinstance(inventory, Mapping):
            entries = [f"{item}={count}" for item, count in list(inventory.items())[:8]]
            facts["inventory_now"] = "inventory now contains " + (", ".join(entries) or "no listed items") + "."
        else:
            facts["inventory_now"] = "Inventory contents were not included in this Body sample."
        goal = compact_facts.get("goal")
        if isinstance(goal, str) and goal:
            facts["body_goal"] = f"The Body is currently pursuing {goal}."
        navigation = compact_facts.get("navigation")
        if isinstance(navigation, Mapping) and navigation:
            parts = []
            if navigation.get("dest") is not None:
                parts.append(f"heading toward {navigation['dest']}")
            if navigation.get("calculating") is True:
                parts.append("calculating a route")
            if navigation.get("path_position") is not None:
                parts.append(f"at route step {navigation['path_position']}")
            failures = navigation.get("repeated_failures")
            if failures:
                parts.append(f"route calculation has failed {failures} times")
            facts["navigation"] = "; ".join(parts) or "navigation state is available but has no active movement."
        construction = compact_facts.get("construction")
        if isinstance(construction, Mapping) and construction:
            matching = construction.get("matching_cells")
            expected = construction.get("expected_cells")
            facts["construction"] = (
                f"construction matches {matching} of {expected} declared cells."
                if matching is not None and expected is not None else "construction evidence is present."
            )
        processes = compact_facts.get("processes")
        if isinstance(processes, Mapping):
            processes = [name for name, active in processes.items() if active is True]
        if isinstance(processes, (list, tuple)):
            facts["processes"] = "Body is actively " + (", ".join(str(item) for item in processes) or "not running a named process") + "."
        vitals = []
        if "health" in compact_facts:
            vitals.append(f"health is {compact_facts['health']}")
        if "oxygen" in compact_facts:
            vitals.append(f"oxygen is {compact_facts['oxygen']}")
        if "food" in compact_facts:
            vitals.append(f"hunger level is {compact_facts['food']}")
        if vitals:
            facts["vitals"] = "; ".join(vitals) + "."
        water = next((compact_facts.get(key) for key in ("in_water", "submerged", "water")
                      if key in compact_facts), None)
        if water is not None:
            facts["water"] = "The Body is in water." if bool(water) else "The Body is not in water."
        safe_position = compact_facts.get("safe_standing_position", compact_facts.get("last_safe_position"))
        if safe_position is not None:
            facts["safe_standing_position"] = f"A previous safe standing position is recorded at {safe_position}."
        if compact_facts.get("food_available") is True:
            facts["food"] = "An edible item is available in the inventory."
        hostile_items = compact_facts.get("nearby_hostiles", compact_facts.get("hostiles"))
        if hostile_items is None:
            hostile_items = compact_facts.get("nearby_entities", compact_facts.get("entities"))
        if isinstance(hostile_items, Mapping):
            hostile_items = hostile_items.get("entities", hostile_items.get("items"))
        if isinstance(hostile_items, list):
            hostile = [item for item in hostile_items if isinstance(item, Mapping)
                       and item.get("hostile") is not False]
            distances = []
            for item in hostile:
                distance = item.get("distance", item.get("dist"))
                if distance is None and isinstance(item.get("dist2"), (int, float)):
                    distance = round(float(item["dist2"]) ** 0.5, 1)
                if isinstance(distance, (int, float)):
                    distances.append(float(distance))
            if hostile:
                nearest = min(distances) if distances else None
                facts["hostiles"] = (
                    f"Nearby hostile entities: {len(hostile)} in the supplied observation"
                    + (f", nearest about {nearest:g} blocks away." if nearest is not None else ".")
                )
            else:
                facts["hostiles"] = "No hostile entities are present in the supplied observation."
        facts["phase"] = phase
        compact = {
            "tool": execution.get("tool"),
            "params": execution.get("params"),
            "state": execution.get("state"),
            "phase": execution.get("phase"),
            "elapsed_s": execution.get("elapsed_s"),
            "since_progress_s": execution.get("since_progress_s"),
            "last_progress_at": execution.get("last_progress_at"),
            "progress_signal": execution.get("progress_signal"),
            "fact_revision": execution.get("fact_revision"),
            "facts": facts,
        }
        return {key: value for key, value in compact.items() if value is not None}

    @staticmethod
    def _compact_result(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"result": str(value)[:500]}
        result = {key: value[key] for key in ("success", "reason", "canRetry", "complete", "outcome") if key in value}
        summary = value.get("summary")
        if isinstance(summary, Mapping):
            result["summary"] = CallSupervisor._bounded(dict(summary), depth=0)
        return result

    @staticmethod
    def _reason_sentence(reason: Any) -> str:
        code = str(reason or "unknown")
        known = {
            "unplannable": "the Body could not find a valid way to complete this operation",
            "crafting_table_select_failed:not_in_inventory": "the required crafting table was not in the inventory",
            "craft_plan_not_available": "no crafting plan was available for the requested result",
        }
        if code.startswith("crafting_table_place_failed:") and "stand_point_unreachable" in code:
            return "the crafting table could not be placed because the required standing point was unreachable"
        return known.get(code, code.replace("_", " "))

    @classmethod
    def _result_sentence(cls, tool: Any, params: Mapping[str, Any], result: Any) -> str:
        name = getattr(tool, "name", "unknown")
        arguments = json.dumps(dict(params), ensure_ascii=False, separators=(",", ":"))
        payload = result if isinstance(result, Mapping) else {}
        reason = payload.get("reason") if isinstance(payload, Mapping) else None
        if payload.get("success") is True:
            return f"{name}({arguments}) has ended normally."
        if reason:
            return f"{name}({arguments}) has ended. It reported failure: {cls._reason_sentence(reason)} ({reason})."
        return f"{name}({arguments}) has ended with no confirmed success."

    @classmethod
    def _sentence_state(
        cls, plan: Mapping[str, Any], body_state: Mapping[str, Any] | None,
        execution: Mapping[str, Any] | None, tool: Any, params: Mapping[str, Any],
        *, result: Any = None, history: Mapping[str, Any] | None = None,
        normal_work: str = "",
    ) -> dict[str, Any]:
        raw = dict(body_state or {})
        compact = cls._compact_execution({**dict(execution or {}), "facts": raw, "params": dict(params)})
        facts = dict(compact.get("facts") or {})
        # Replay rows already contain the human sentence fields. Live snapshots
        # are converted by _compact_execution into the same vocabulary.
        state: dict[str, Any] = {
            "task": str(plan.get("goal") or ""),
            "agent_intent": str(plan.get("intent") or plan.get("node_description") or ""),
        }
        aliases = {
            "inventory": ("inventory", "inventory_now"),
            "body": ("body", "vitals"),
            "threats": ("threats", "hostiles"),
            "position": ("where", "position"),
            "progress": ("progress", "last_progress"),
        }
        for target, keys in aliases.items():
            if result is not None and target == "progress":
                continue
            value = next((raw.get(k) for k in keys
                          if raw.get(k) not in (None, "")
                          and not (target == "inventory" and isinstance(raw.get(k), Mapping))
                          and not (target == "position" and isinstance(raw.get(k), (list, tuple)))), None)
            if value is None:
                value = next((facts.get(k) for k in keys if facts.get(k) not in (None, "")), None)
            if value is not None:
                state[target] = value if isinstance(value, str) else str(value)
        if facts.get("quantity"):
            state["quantity"] = str(facts["quantity"])
        if result is None:
            value = raw.get("current_work") or facts.get("doing")
            if value:
                state["current_work"] = value
        else:
            state["finished_call"] = cls._result_sentence(tool, params, result)
            if history:
                recent = history.get("recent_results") if isinstance(history, Mapping) else None
                earlier = history.get("earlier") if isinstance(history, Mapping) else ""
                if isinstance(recent, list):
                    rendered = [
                        item if isinstance(item, str) else cls._result_sentence(tool, params, item)
                        for item in recent
                    ]
                    if result is not None:
                        current_sentence = cls._result_sentence(tool, params, result)
                        if rendered and rendered[-1] == current_sentence:
                            rendered = rendered[:-1]
                    history_lines = ([earlier] if earlier else []) + rendered
                    if history_lines:
                        state["earlier_attempts_of_this_call"] = history_lines
        if normal_work:
            state["normal_work"] = normal_work[:800]
        # Compose each survival fact once. A previous value is evidence of a
        # change only when the current value actually moved in the relevant
        # direction; it is not itself evidence of harm or of being underwater.
        vital_lines: list[str] = []
        health = raw.get("health")
        health_previous = raw.get("health_previous")
        if isinstance(health, (int, float)):
            vital_lines.append(f"health is {health} of 20.")
            if isinstance(health_previous, (int, float)) and health < health_previous:
                vital_lines.append(f"Health fell from {health_previous} to {health} since the previous sample.")
        food = raw.get("food")
        if isinstance(food, (int, float)):
            vital_lines.append(f"hunger level is {food} of 20.")
        oxygen = raw.get("oxygen")
        oxygen_max = raw.get("oxygen_max", 300)
        oxygen_previous = raw.get("oxygen_previous")
        if isinstance(oxygen, (int, float)):
            if oxygen < oxygen_max:
                if isinstance(oxygen_previous, (int, float)) and oxygen != oxygen_previous:
                    vital_lines.append(f"oxygen changed from {oxygen_previous} to {oxygen} of {oxygen_max} since the previous sample; the head is underwater.")
                else:
                    vital_lines.append(f"oxygen is {oxygen} of {oxygen_max}; the head is underwater.")
            else:
                vital_lines.append(f"oxygen is full at {oxygen_max}.")
        if vital_lines:
            state["body"] = " ".join(vital_lines)
        return {key: value for key, value in state.items() if value not in (None, "")}

    @staticmethod
    def _intent_summary(plan: Mapping[str, Any]) -> str:
        intent = str(plan.get("intent") or plan.get("node_description") or "").strip()
        step = str(plan.get("current_step") or "").strip()
        goal = str(plan.get("goal") or "").strip()
        parts = []
        if intent:
            parts.append(f"purpose: {intent}")
        if step and step != intent:
            parts.append(f"current step: {step}")
        if goal and goal not in {intent, step}:
            parts.append(f"goal: {goal}")
        return "; ".join(parts)[:900]

    @staticmethod
    def _question_set(criteria: Mapping[str, str]) -> dict[str, Any]:
        danger_keys = {"surface", "move_away", "retreat_safe"}
        protective = {key: value for key, value in criteria.items() if key in danger_keys}
        normal = {key: value for key, value in criteria.items() if key not in danger_keys}
        if not protective:
            return {"q": {"type": "choice", "instructions": "Choose one supplied option.", "criteria": normal}}
        return {
            "danger": {
                "type": "choice",
                "instructions": "Is the Body in danger?",
                "criteria": {
                    "safe": "Nothing is harming the Body and nothing nearby is about to harm it.",
                    "in_danger": "The Body is being harmed right now, or something nearby is about to harm it.",
                },
            },
            "normal": {
                "type": "choice",
                "instructions": "If safe, choose one normal work option.",
                "criteria": normal,
            },
            "protective": {
                "type": "choice",
                "instructions": "If dangerous, choose one concrete protective action.",
                "criteria": {**protective, "call_agent": criteria.get("call_agent", "Stop and return control to the Agent.")},
            },
        }

    @staticmethod
    def _answer_for_questions(raw: Mapping[str, Any] | None, fallback: str,
                              questions: Mapping[str, Any]) -> str:
        answers = raw.get("answers") if isinstance(raw, Mapping) else None
        if not isinstance(answers, Mapping):
            return fallback
        allowed = {str(key) for question in questions.values() for key in (question.get("criteria") or {})}
        danger = answers.get("danger")
        if isinstance(danger, Mapping):
            danger_choice = str(danger.get("choice") or "")
            if danger_choice in {"danger", "in_danger", "unsafe", "yes"}:
                selected = answers.get("protective")
                return str(selected.get("choice")) if isinstance(selected, Mapping) and selected.get("choice") else (fallback if fallback in allowed else "unknown")
            if danger_choice in {"safe", "no"}:
                selected = answers.get("normal")
                return str(selected.get("choice")) if isinstance(selected, Mapping) and selected.get("choice") else (fallback if fallback in allowed else "unknown")
        allowed = {str(key) for question in questions.values() for key in (question.get("criteria") or {})}
        for key in ("q", "normal", "protective"):
            selected = answers.get(key)
            if isinstance(selected, Mapping) and selected.get("choice"):
                return str(selected["choice"])
        return fallback if fallback in allowed else (fallback if fallback in allowed else "unknown")

    def _question(
        self, call_id: str, tool: Any, params: Mapping[str, Any],
        execution: Mapping[str, Any], *, idle_s: float, purpose: str,
        supervision_context: Mapping[str, Any] | None,
        options: Mapping[str, str] | None = None,
        decision_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = self._plan_context(supervision_context=supervision_context, tool=tool, params=params)
        # Keep the semantic fields when they carry different facts, but do not
        # spend every frame repeating the same sentence under four plan keys.
        question_plan = dict(plan)
        intent = str(question_plan.get("intent") or "")
        if intent:
            for key in ("goal", "current_step", "node_description"):
                if question_plan.get(key) == intent:
                    question_plan.pop(key, None)
        if isinstance(decision_context, Mapping):
            facts = dict(decision_context)
            body_state = facts.get("body_state", execution.get("facts", {}))
            if isinstance(body_state, Mapping):
                body_state = self._with_vital_history(call_id, body_state)
                if not any(key in body_state for key in ("body", "inventory", "threats", "current_work", "progress", "position")):
                    body_state = self._compact_execution({"facts": body_state})["facts"]
            result_frame = purpose in {"result_frame", "call_result"} or "tool_result" in facts
            if result_frame:
                # The tool has returned.  Do not carry the running-call
                # narration into a terminal result question.
                body_state.pop("doing", None)
                if str(body_state.get("last_progress", "")).endswith("since the call began."):
                    body_state.pop("last_progress", None)
                if body_state.get("phase") == "working":
                    body_state.pop("phase", None)
            execution_facts = {} if result_frame else self._compact_execution(execution)
            state = self._sentence_state(
                question_plan, body_state, execution, tool,
                facts.get("tool_arguments", params),
                result=facts.get("tool_result", {}),
                history=facts.get("history"),
                normal_work=self._explanation(tool),
            )
            request = {
                "state": state,
                "questions": self._question_set(self._criteria(
                    plan, options, tool_name=str(getattr(tool, "name", "unknown")),
                    normal_work=self._explanation(tool),
                )),
            }
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)
            request_chars_before = len(encoded)
            if request_chars_before > MAX_QUESTION_CHARS:
                history = request["state"].get("earlier_attempts_of_this_call")
                while isinstance(history, list) and len(history) > 2 and len(json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)) > MAX_QUESTION_CHARS - 512:
                    history.pop(0)
                if len(json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)) > MAX_QUESTION_CHARS - 512:
                    request["state"].pop("inventory", None)
                encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)
            request["_request_chars_before"] = request_chars_before
            request["state"]["request_chars"] = len(encoded)
            return request
        execution_for_question = dict(execution)
        execution_facts = execution_for_question.get("facts")
        if isinstance(execution_facts, Mapping):
            execution_facts = self._with_vital_history(call_id, execution_facts)
            execution_for_question["facts"] = execution_facts
        state = self._sentence_state(
            question_plan, execution_for_question.get("facts") or {},
            execution_for_question, tool, params,
            normal_work=self._explanation(tool),
        )
        request: dict[str, Any] = {
            "state": state,
            "questions": self._question_set(self._criteria(
                    plan, options, tool_name=str(getattr(tool, "name", "unknown")),
                    normal_work=self._explanation(tool),
                )),
        }
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)
        request_chars_before = len(encoded)
        if request_chars_before > MAX_QUESTION_CHARS:
            history = request["state"].get("earlier_attempts_of_this_call")
            while isinstance(history, list) and len(history) > 2 and len(json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)) > MAX_QUESTION_CHARS - 512:
                history.pop(0)
            if len(json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)) > MAX_QUESTION_CHARS - 512:
                request["state"].pop("inventory", None)
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)
        # This private value is removed before the request reaches Decision;
        # the trace reports both sides of compaction.
        request["_request_chars_before"] = request_chars_before
        request["state"]["request_chars"] = len(encoded)
        return request

    async def judge(
        self, call_id: str, tool: Any, params: Mapping[str, Any],
        execution: Mapping[str, Any], idle_s: float, *,
        purpose: str = "running_frame",
        supervision_context: Mapping[str, Any] | None = None,
        options: Mapping[str, str] | None = None,
        decision_context: Mapping[str, Any] | None = None,
    ) -> str:
        request = self._question(
            call_id, tool, params, execution, idle_s=idle_s,
            purpose=purpose, supervision_context=supervision_context, options=options,
            decision_context=decision_context,
        )
        tool_name = getattr(tool, "name", "unknown")
        request_chars_before = int(request.pop("_request_chars_before", request["state"].get("request_chars", 0)))
        request_chars = int(request["state"].get("request_chars", 0))
        fact_revision = execution.get("fact_revision")
        self.runtime.trace.emit(
            "call_supervision_request", tool_call_id=call_id, tool=tool_name,
            purpose=purpose, idle_s=round(idle_s, 3), fact_revision=fact_revision,
            request_chars_before=request_chars_before, request_chars=request_chars,
            request=request,
        )
        started = time.monotonic()
        raw: dict[str, Any] | None = None
        try:
            answer = await self._client.ask(request, priority=DecisionPriority.SUPERVISION)
            raw = answer.raw
            choice = self._answer_for_questions(
                raw, answer.choice, request["questions"]
            )
            allowed = set()
            for question in request["questions"].values():
                allowed.update((question.get("criteria") or {}).keys())
            choice = {"step_done": "next_step", "continue": "keep_going", "go_to_surface": "surface", "stay": "unknown"}.get(choice, choice)
            choice = choice if choice in allowed else "unknown"
        except Exception:
            choice = "unknown"
        elapsed = time.monotonic() - started
        self.runtime.trace.emit(
            "call_supervision_result", tool_call_id=call_id, tool=tool_name,
            purpose=purpose, response=raw, choice=choice,
            unknown=choice == "unknown", elapsed_s=elapsed,
            decision_ms=round(elapsed * 1000.0, 3), fact_revision=fact_revision,
        )
        return choice

    async def _judge_frame(
        self, call_id: str, tool: Any, params: Mapping[str, Any],
        execution: Mapping[str, Any], idle_s: float, *, purpose: str,
        supervision_context: Mapping[str, Any] | None,
        options: Mapping[str, str] | None = None,
        decision_context: Mapping[str, Any] | None = None,
    ) -> str:
        """Call the overridable judge while accepting slice-1 test seams."""
        signature = inspect.signature(self.judge)
        accepts_context = (
            "purpose" in signature.parameters
            or "supervision_context" in signature.parameters
            or "options" in signature.parameters
            or "decision_context" in signature.parameters
            or any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
        )
        if accepts_context:
            return await self.judge(
                call_id, tool, params, execution, idle_s,
                purpose=purpose, supervision_context=supervision_context, options=options,
                decision_context=decision_context,
            )
        return await self.judge(call_id, tool, params, execution, idle_s)

    async def _bounded_read(self, record: dict[str, Any], tool: Any,
                            params: Mapping[str, Any]) -> dict[str, Any]:
        reader = getattr(tool, "supervision_reader", None)
        if not callable(reader):
            # Generic body reads repeat the same broad state and are not a
            # Decision option.  The execution snapshot already carries the
            # bounded state facts for every cadence frame.
            return {}
        try:
            if callable(reader):
                value = await self.runtime._read_lane.run(reader, dict(params))
                source = "registered_reader"
            else:
                body = getattr(self.runtime, "body", None)
                getter = getattr(body, "get_state", None)
                if not callable(getter):
                    return {}
                value = await self.runtime._read_lane.run(getter)
                source = "body_state"
        except Exception as exc:
            self.runtime.trace.emit(
                "call_supervision_read_unknown", tool_call_id=record["call_id"],
                tool=getattr(tool, "name", "unknown"), error_type=type(exc).__name__,
            )
            return {}
        if isinstance(value, Mapping):
            facts = dict(value)
        elif is_dataclass(value):
            facts = asdict(value)
        else:
            raw = getattr(value, "__dict__", None)
            facts = dict(raw) if isinstance(raw, dict) else {}
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        before = record.get("last_read_facts")
        if before == self._bounded(facts):
            return {}
        record["last_read_facts"] = self._bounded(facts)
        if snapshots is not None:
            snapshots.enrich(record["call_id"], facts, source=source)
        record["reads"] += 1
        record["reader_new_fact"] = True
        self.runtime.trace.emit(
            "call_supervision_read", tool_call_id=record["call_id"],
            tool=getattr(tool, "name", "unknown"), read_number=record["reads"],
            facts=self._bounded(facts),
        )
        return facts

    @staticmethod
    def _facts_for_options(execution: Mapping[str, Any]) -> Mapping[str, Any]:
        facts = execution.get("facts")
        return facts if isinstance(facts, Mapping) else {}

    @classmethod
    def _options_for(
        cls, tool: Any, record: Mapping[str, Any], supplied: Mapping[str, str] | None = None,
        execution: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        """Compile concrete work and safety choices from generic facts."""
        current = execution or {}
        terminal = str(current.get("state") or "") in {"succeeded", "failed", "cancelled", "unknown"}
        context = record.get("supervision_context")
        context = context if isinstance(context, Mapping) else {}
        options = compile_work_options(
            tool=str(record.get("tool") or getattr(tool, "name", "unknown")),
            params=record.get("params") if isinstance(record.get("params"), Mapping) else {},
            intent=str(context.get("intent") or context.get("node_description") or ""),
            next_call=str(context.get("next_call") or ""),
            running=not terminal,
            result=current.get("result") if isinstance(current.get("result"), Mapping) else None,
            include_inspect=bool(callable(getattr(tool, "supervision_reader", None)) and record.get("reader_new_fact")),
        )
        if supplied is not None:
            # Tree-provided options are semantic alternatives supplied by the
            # Agent; the runtime still compiles every base option here.
            options.update({str(key): str(value) for key, value in supplied.items()})
        facts = cls._facts_for_options(execution or {})
        water = any(facts.get(key) is True for key in ("in_water", "submerged", "water"))
        if water or (isinstance(facts.get("oxygen"), (int, float)) and facts.get("oxygen") < facts.get("oxygen_max", 300)):
            options.setdefault("surface", _DEFAULT_ACTION_TEXT["go_to_surface"])
        if facts.get("safe_standing_position") is not None or facts.get("last_safe_position") is not None:
            if facts.get("recently_hurt") is True or facts.get("health_drop") is not None:
                options.setdefault("retreat_safe", _DEFAULT_ACTION_TEXT["retreat_safe"])
        if facts.get("food_available") is True and isinstance(facts.get("food"), (int, float)) and facts.get("food") < facts.get("food_max", 20):
            options.setdefault("eat", _DEFAULT_ACTION_TEXT["eat"])
        hostile = facts.get("nearby_hostiles", facts.get("hostiles"))
        if hostile is None:
            hostile = facts.get("nearby_entities", facts.get("entities"))
        if isinstance(hostile, Mapping):
            hostile = hostile.get("entities", hostile.get("items"))
        close_hostile = False
        if isinstance(hostile, list):
            for item in hostile:
                if not isinstance(item, Mapping) or item.get("hostile") is False:
                    continue
                distance = item.get("distance", item.get("dist"))
                if distance is None and isinstance(item.get("dist2"), (int, float)):
                    distance = float(item["dist2"]) ** 0.5
                if isinstance(distance, (int, float)) and float(distance) <= MOVE_AWAY_APPLICABLE_DISTANCE:
                    close_hostile = True
                    break
        if close_hostile or facts.get("recently_hurt") is True:
            options.setdefault("move_away", _DEFAULT_ACTION_TEXT["move_away"])
        if callable(getattr(tool, "supervision_reader", None)) and record.get("reader_new_fact"):
            options["inspect"] = "Read one new registered fact."
        return options

    @staticmethod
    def _tree_options(context: Mapping[str, Any] | None) -> Mapping[str, str] | None:
        if not isinstance(context, Mapping):
            return None
        for key in ("node_options", "tree_options", "options"):
            value = context.get(key)
            if isinstance(value, Mapping):
                return {str(option): str(description) for option, description in value.items()}
            if isinstance(value, list):
                compiled: dict[str, str] = {}
                for item in value:
                    if isinstance(item, Mapping) and item.get("id"):
                        compiled[str(item["id"])] = str(item.get("intent") or item.get("description") or "")
                if compiled:
                    return compiled
        return None

    @classmethod
    def _default_action(cls, choice: str, execution: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        facts = cls._facts_for_options(execution)
        if choice in {"surface", "go_to_surface"}:
            return "go_to_surface", {"timeout_s": 120.0}
        if choice == "retreat_safe":
            pos = facts.get("safe_standing_position", facts.get("last_safe_position"))
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                return "move_to", {"pos": [int(float(value)) for value in pos], "radius": 1, "timeout_s": 30.0}
        if choice == "eat":
            item = facts.get("food_item")
            if isinstance(item, str) and item.strip():
                return "consume_item", {"item": item.strip()}
        if choice == "move_away":
            pos = facts.get("nearest_hostile_position")
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                return "move_away", {"danger_pos": [float(value) for value in pos], "min_distance": 8.0}
        return None

    @staticmethod
    def _deterministic_terminal(execution: Mapping[str, Any], read: Mapping[str, Any]) -> dict[str, Any] | None:
        """Preserve the existing build_structure material evidence rule."""
        if not read.get("complete") or not read.get("remaining_cells"):
            return None
        facts = execution.get("facts") if isinstance(execution.get("facts"), Mapping) else {}
        if facts.get("builder_paused") is not True or facts.get("creative") is True:
            return None
        if facts.get("inventory_complete") is not True or facts.get("inventory_includes_selected") is not True:
            return None
        construction = facts.get("construction") if isinstance(facts.get("construction"), Mapping) else {}
        item, inventory = construction.get("material_item"), facts.get("inventory")
        if not isinstance(item, str) or not item or not isinstance(inventory, Mapping):
            return None
        if inventory.get(item, 0) != 0:
            return None
        return {"reason": "material_exhausted", "confirmed": True, "construction": read}

    @staticmethod
    def _handoff_facts(
        execution: Mapping[str, Any], *, reason: str, confirmed: bool,
        decision: str | None, plan: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        facts = execution.get("facts") if isinstance(execution.get("facts"), Mapping) else {}
        evidence = {
            key: facts.get(key) for key in
            ("position", "navigation", "pathing", "processes", "builder_paused", "goal")
            if key in facts
        }
        construction = facts.get("construction")
        if isinstance(construction, Mapping):
            evidence["construction"] = {
                key: value for key, value in construction.items() if key != "material_item"
            }
        safe: dict[str, Any] = {
            "reason": reason, "confirmed": confirmed,
            "what": {
                "tool": execution.get("tool"), "phase": execution.get("phase"),
                "elapsed_s": execution.get("elapsed_s"),
                "no_progress_s": execution.get("since_progress_s"),
                "last_progress_at": execution.get("last_progress_at"),
                "progress_signal": execution.get("progress_signal"),
            },
            "body": {"position": facts.get("position")},
            "decision": {"choice": decision, "evidence": evidence},
        }
        if isinstance(plan, Mapping):
            safe["plan"] = {
                key: plan.get(key) for key in
                ("goal", "current_step", "node_description", "intent_chain")
                if plan.get(key)
            }
        if isinstance(extra, Mapping) and isinstance(extra.get("construction"), Mapping):
            construction = dict(extra["construction"])
            construction.pop("material_item", None)
            safe["construction"] = construction
        if isinstance(extra, Mapping) and isinstance(extra.get("tool_result"), Mapping):
            safe["result"] = CallSupervisor._compact_result(extra["tool_result"])
        if isinstance(extra, Mapping) and isinstance(extra.get("history"), str):
            safe["history"] = extra["history"][:900]
        return CallSupervisor._bounded(safe)

    async def run(
        self, tool: Any, callback: Any, args: tuple[Any, ...], *, call_id: str,
        timeout_s: float | None, supervision_context: Mapping[str, Any] | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        record: dict[str, Any] = {
            "call_id": call_id, "action_id": None, "cancel_requested": False,
            "settling": False, "reads": 0, "questions": 0, "unknown_answers": 0,
            "supervision_context": dict(supervision_context or {}),
            "tool": getattr(tool, "name", "unknown"),
            "params": dict(args[1]) if len(args) > 1 and isinstance(args[1], Mapping) else {},
            "started_at": time.monotonic(),
            "initial_reader_done": False,
            "reader_new_fact": False,
        }
        self.current = record

        def receive(payload: Mapping[str, Any]) -> None:
            if self.current is record and payload.get("call_id") == call_id:
                record["action_id"] = payload.get("action_id")
                wake.set()

        telemetry = getattr(self.runtime, "panel_telemetry", None)
        snapshots = getattr(self.runtime, "execution_snapshots", None)

        def notify(payload: Mapping[str, Any]) -> None:
            if snapshots is not None:
                snapshots.observe(call_id, payload)
            if telemetry is not None:
                telemetry.observe(call_id, payload)
            loop.call_soon_threadsafe(receive, {**dict(payload), "call_id": call_id})

        def invoke() -> Any:
            if snapshots is not None:
                snapshots.running(call_id)
            with tool_observation_scope(call_id, notify):
                return callback(*args)

        worker = asyncio.create_task(self.runtime.execution_lane.run(invoke, timeout_s=timeout_s))
        params = args[1] if len(args) > 1 and isinstance(args[1], Mapping) else {}
        monitor: asyncio.Task[Any] | None = None
        try:
            grace = min(FAST_COMPLETION_GRACE_S, max(0.0, self.suspicion_s))
            if grace:
                done, _ = await asyncio.wait({worker}, timeout=grace)
                if not done:
                    monitor = asyncio.create_task(self._monitor(record, tool, params, wake, worker))
                    record["monitor_task"] = monitor
            else:
                monitor = asyncio.create_task(self._monitor(record, tool, params, wake, worker))
                record["monitor_task"] = monitor
            result = await worker
            if monitor is not None:
                await monitor
                monitor = None
            # A direct call is a one-node work tree.  Its terminal result is a
            # fact; plan calls are judged by WorkPlanExecutor, the sole owner
            # of repeat.  Direct calls ask one result question and never
            # replay the Body invocation inside this supervisor.
            if (not record["cancel_requested"]
                    and getattr(self.runtime, "reflex_manager", None) is not None
                    and not bool(record["supervision_context"].get("is_plan"))):
                await self._decide_result(record, tool, params, result)
            handoff = self.runtime.direct_call_handoffs.get(call_id)
            payload = result if isinstance(result, dict) else getattr(result, "result", None)
            if handoff is not None and isinstance(payload, dict):
                payload.setdefault("metrics", {})["direct_call_handoff"] = handoff
            metrics = payload.get("metrics") if isinstance(payload, dict) else None
            if isinstance(metrics, dict) and metrics.get("control_unknown"):
                self.runtime.body_control.mark_unknown()
            return result
        finally:
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            decision_task = record.get("decision_task")
            if isinstance(decision_task, asyncio.Task) and not decision_task.done():
                decision_task.cancel()
                await asyncio.gather(decision_task, return_exceptions=True)
            self.runtime.trace.emit(
                "call_supervision_finished", tool_call_id=call_id,
                tool=getattr(tool, "name", "unknown"), reads=record["reads"],
                questions=record["questions"], unknown_answers=record.get("unknown_answers", 0),
                elapsed_s=round(time.monotonic() - record["started_at"], 3),
                requests_per_min=round(
                    record["questions"] / max(time.monotonic() - record["started_at"], 0.001) * 60.0, 3,
                ),
            )
            if self.current is record:
                self.current = None

    async def _decide_result(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any], result: Any,
    ) -> str:
        execution = self._snapshot(record["call_id"])
        if not execution:
            execution = {"tool": getattr(tool, "name", "unknown"), "facts": {}}
        payload = result if isinstance(result, Mapping) else getattr(result, "result", result)
        if not isinstance(payload, Mapping):
            payload = {"result": str(payload)[:500]}
        options = compile_work_options(
            tool=str(getattr(tool, "name", "unknown")),
            params=params,
            intent=str(record.get("supervision_context", {}).get("intent") or record.get("supervision_context", {}).get("node_description") or ""),
            running=False,
            result=dict(payload),
        )
        context = {
            "tool_result": dict(payload),
            "tool_arguments": dict(params),
            "body_state": dict(execution.get("facts") or {}),
            "execution": execution,
            "reads": [],
        }
        try:
            # A terminal result must never hold the Body or an Agent turn
            # indefinitely when the Decision endpoint is unavailable.
            choice = await asyncio.wait_for(self._judge_frame(
                record["call_id"], tool, params, execution,
                float(execution.get("since_progress_s") or 0.0),
                purpose="result_frame", supervision_context=record["supervision_context"],
                options=options, decision_context=context,
            ), timeout=2.2)
        except asyncio.TimeoutError:
            choice = "unknown"
        record["questions"] += 1
        if choice == "unknown":
            record["unknown_answers"] += 1
            return "unknown"
        choice = {"next_step": "step_done", "keep_going": "repeat"}.get(choice, choice)
        if choice in {"step_done", "repeat", "call_agent"}:
            record["decision"] = choice
            # The Body is already terminal.  Store the result judgement for
            # the caller without routing a second stop through the running
            # call path (and without rewriting a real terminal result).
            execution = self._snapshot(record["call_id"]) or execution
            safe = self._handoff_facts(
                execution, reason=f"decision_{choice}",
                confirmed=choice == "step_done", decision=choice,
                plan=self._plan_context(
                    supervision_context=record["supervision_context"],
                    tool=tool, params=params,
                ),
                extra={"tool_result": self._compact_result(payload)},
            )
            self.runtime.trace.emit("direct_call_handoff", tool_call_id=record["call_id"],
                                    facts=safe, settlement={"settled": True, "terminal": True})
            self.runtime.direct_call_handoffs[record["call_id"]] = {
                "facts": safe, "settlement": {"settled": True, "terminal": True},
            }
            if not bool(record["supervision_context"].get("is_plan")):
                notify = getattr(self.runtime, "notify_reflex_fact", None)
                if callable(notify):
                    notify({"call_id": record["call_id"], "status": "handoff_requested", **safe})
        return choice

    async def _monitor(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
        wake: asyncio.Event, worker: asyncio.Task[Any],
    ) -> None:
        last_revision = -1
        decision_task: asyncio.Task[str] | None = None
        pending: tuple[dict[str, Any], float, str] | None = None
        decision_revision = -1
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        while not record["cancel_requested"]:
            if worker.done() and decision_task is None:
                return
            wake_wait = asyncio.create_task(wake.wait())
            clock_wait = asyncio.create_task(asyncio.sleep(FACT_FRAME_INTERVAL_S))
            wait_set: set[asyncio.Future[Any]] = {wake_wait}
            wait_set.add(clock_wait)
            if decision_task is not None:
                wait_set.add(decision_task)
            if not worker.done():
                wait_set.add(worker)
            done, pending_tasks = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            finished_decision = decision_task if decision_task is not None and decision_task in done else None
            if clock_wait in done and not worker.done() and snapshots is not None:
                snapshots.tick(record["call_id"])
            if wake_wait in done or clock_wait in done:
                wake.clear()
                execution = self._snapshot(record["call_id"])
                if execution:
                    revision = int(execution.get("fact_revision") or 0)
                    phase_changed = execution.get("phase") != record.get("last_phase")
                    record["last_phase"] = execution.get("phase")
                    if revision > last_revision and (phase_changed or revision != last_revision):
                        last_revision = revision
                        idle_s = float(execution.get("since_progress_s") or 0.0)
                        frame = (execution, idle_s, "running_frame")
                        if (not record["initial_reader_done"]
                                and callable(getattr(tool, "supervision_reader", None))):
                            # Establish one bounded reader fact before the
                            # first question.  The reader option is therefore
                            # present only after a real, dedicated read has
                            # returned new evidence.
                            record["initial_reader_done"] = True
                            await self._bounded_read(record, tool, params)
                            execution = self._snapshot(record["call_id"]) or execution
                        frame_options = self._options_for(
                            tool, record,
                            supplied=self._tree_options(record["supervision_context"]),
                            execution=execution,
                        )
                        if decision_task is not None and not decision_task.done():
                            # There is no queue: only the latest frame survives.
                            pending = frame
                        elif decision_task is None and execution.get("state") not in {"succeeded", "failed", "cancelled", "unknown"}:
                            decision_revision = revision
                            decision_task = asyncio.create_task(self._judge_frame(
                                record["call_id"], tool, params, execution, idle_s,
                                purpose="running_frame",
                                supervision_context=record["supervision_context"], options=frame_options,
                            ))
                            record["decision_task"] = decision_task
            if finished_decision is not None:
                try:
                    choice = finished_decision.result()
                except Exception:
                    choice = "unknown"
                if decision_task is finished_decision:
                    decision_task = None
                record["questions"] += 1
                if choice == "unknown":
                    record["unknown_answers"] += 1
                execution = self._snapshot(record["call_id"]) or (pending[0] if pending else {})
                if choice == "inspect" and record["reads"] < self.max_reads:
                    record["reader_new_fact"] = False
                    read = await self._bounded_read(record, tool, params)
                    blocker = self._deterministic_terminal(
                        self._snapshot(record["call_id"]) or execution, read,
                    )
                    if blocker is not None:
                        await self._handoff(
                            record, self._snapshot(record["call_id"]) or execution,
                            blocker, decision=None,
                            plan=self._plan_context(
                                supervision_context=record["supervision_context"],
                                tool=tool, params=params,
                            ),
                        )
                        return
                    # The read is consumed by the next cadence frame.  It must
                    # not create a burst of questions faster than the frame
                    # clock, especially when a reader returns the same sample.
                elif choice == "inspect":
                    self.runtime.trace.emit(
                        "call_supervision_limit", tool_call_id=record["call_id"],
                        tool=getattr(tool, "name", "unknown"), kind="reads", limit=self.max_reads,
                    )
                elif choice in {"step_done", "next_step", "call_agent"}:
                    record["decision"] = choice
                    plan = self._plan_context(
                        supervision_context=record["supervision_context"],
                        tool=tool, params=params,
                    )
                    normalized_choice = "step_done" if choice in {"step_done", "next_step"} else choice
                    record["decision"] = normalized_choice
                    reason = "decision_step_done" if normalized_choice == "step_done" else "decision_call_agent"
                    await self._handoff(
                        record, execution,
                        {"reason": reason, "confirmed": normalized_choice == "step_done"},
                        decision=choice, plan=plan,
                    )
                    return
                elif choice in {"surface", "go_to_surface", "retreat_safe", "eat", "move_away"}:
                    action = self._default_action(choice, execution)
                    if action is not None:
                        await self._run_default_action(record, execution, choice, action)
                        return
                    # A stale or incomplete frame cannot supply safe action
                    # parameters.  Treat it as unknown and leave the Body
                    # untouched.
                    self.runtime.trace.emit(
                        "call_supervision_default_unknown", tool_call_id=record["call_id"],
                        choice=choice,
                    )
                if pending is not None:
                    frame = pending
                    pending = None
                    if int(frame[0].get("fact_revision") or -1) > decision_revision:
                        decision_revision = int(frame[0].get("fact_revision") or -1)
                        decision_task = asyncio.create_task(self._judge_frame(
                            record["call_id"], tool, params, frame[0], frame[1],
                            purpose=frame[2], supervision_context=record["supervision_context"],
                            options=self._options_for(
                                tool, record,
                                supplied=self._tree_options(record["supervision_context"]),
                                execution=frame[0],
                            ),
                        ))
                        record["decision_task"] = decision_task
            if worker in done and decision_task is None:
                for waiter in (wake_wait, clock_wait):
                    if waiter not in done:
                        waiter.cancel()
                await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)
                return
            if wake_wait not in done and decision_task is None and worker.done():
                for waiter in (wake_wait, clock_wait):
                    if waiter not in done:
                        waiter.cancel()
                await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)
                return
            for waiter in (wake_wait, clock_wait):
                if waiter not in done:
                    waiter.cancel()
            await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)

    async def supervise_idle(
        self, tool: Any, *, call_id: str, plan_context: Mapping[str, Any] | None = None,
        duration_s: float | None = None, frame_interval_s: float = 0.2,
        options: Mapping[str, str] | None = None, stop_on_choice: bool = False,
        notify_agent: bool = True,
        decision_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ask at a bounded cadence while an unfinished tree is idle.

        Step 1 exposes this loop for the plan runtime to consume in Step 2. It
        calls Agent after a small same-position unknown streak; it never stops
        a running call on unknown.
        """
        started = time.monotonic()
        previous_position: Any = object()
        unknown_same_position = 0
        counts = {"questions": 0, "unknown": 0, "agent": 0}
        while duration_s is None or time.monotonic() - started < duration_s:
            execution = self._snapshot(call_id)
            if not execution:
                break
            position = (execution.get("facts") or {}).get("position")
            if position != previous_position:
                previous_position = position
                unknown_same_position = 0
            choice = await self._judge_frame(
                call_id, tool, {}, execution,
                float(execution.get("since_progress_s") or 0.0),
                purpose="idle_frame", supervision_context=plan_context, options=options,
                decision_context=decision_context,
            )
            counts["questions"] += 1
            if choice == "unknown":
                counts["unknown"] += 1
                unknown_same_position += 1
                if unknown_same_position >= self.idle_unknown_limit:
                    counts["agent"] += 1
                    if stop_on_choice:
                        counts["choice"] = "call_agent"
                    facts = self._handoff_facts(
                        execution, reason="idle_unknown_limit", confirmed=False,
                        decision="unknown", plan=plan_context,
                    )
                    self.runtime.trace.emit(
                        "call_supervision_idle_agent", tool_call_id=call_id,
                        reason="same_position_unknown_limit", limit=self.idle_unknown_limit,
                    )
                    notify = getattr(self.runtime, "notify_reflex_fact", None)
                    if notify_agent and callable(notify):
                        notify({
                            "kind": "supervision_idle",
                            "activity_id": call_id,
                            "tool_call_id": call_id,
                            "status": "call_agent",
                            "facts": facts,
                        })
                    break
            else:
                unknown_same_position = 0
                if stop_on_choice and choice != "unknown":
                    counts["choice"] = choice
                    return counts
            await asyncio.sleep(max(0.0, frame_interval_s))
        if stop_on_choice:
            counts["choice"] = "unknown"
        return counts

    async def _handoff(
        self, record: dict[str, Any], execution: Mapping[str, Any],
        details: Mapping[str, Any], *, decision: str | None,
        plan: Mapping[str, Any] | None = None,
    ) -> None:
        if self.current is not record or record["cancel_requested"]:
            return
        record["cancel_requested"] = record["settling"] = True
        call_id = record["call_id"]
        safe = self._handoff_facts(
            self._snapshot(call_id) or execution,
            reason=str(details.get("reason") or "decision_call_agent"),
            confirmed=bool(details.get("confirmed")), decision=decision,
            plan=plan, extra=details,
        )
        self.runtime.trace.emit("direct_call_blocked", tool_call_id=call_id, facts=safe)
        self.runtime.trace.emit(
            "call_supervision_stop_requested", tool_call_id=call_id,
            tool=execution.get("tool"), reason=safe["reason"], decision=decision,
        )
        # The existing stop/settlement entry remains the only stop owner. Do
        # not revoke the containing plan: step_done must let it advance.
        is_plan = bool(record["supervision_context"].get("is_plan"))
        started = time.monotonic()
        settled = await self.runtime.cancel_active_execution_with_facts(
            "direct_call:" + safe["reason"], cancel_reflex=False,
            # A plan handoff stops the current node but does not invalidate
            # the containing activity. Direct calls have no owner to preserve
            # and keep the existing generation invalidation safety boundary.
            invalidate_generation=not is_plan,
        )
        self.runtime.trace.emit(
            "call_supervision_stop_settled", tool_call_id=call_id,
            elapsed_s=time.monotonic() - started,
            settled=bool(settled.get("settled")) if isinstance(settled, Mapping) else None,
        )
        self.runtime.trace.emit(
            "direct_call_handoff", tool_call_id=call_id, facts=safe, settlement=settled,
        )
        self.runtime.direct_call_handoffs[call_id] = {"facts": safe, "settlement": settled}
        # Only after the unified stop path has settled may a direct call send
        # its fact to the Agent. Plan calls are reported once by
        # ReflexManager, after WorkPlanExecutor records the node state; doing
        # both here and there would create two facts for one stopped node.
        if not is_plan:
            # Use the runtime's unified delivery path.  Responses sessions
            # forward this to the kernel; SDK sessions forward it to their
            # outer AgentSession listener.  Calling the kernel directly here
            # silently drops the handoff for SDK direct calls.
            notify_fact = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(notify_fact):
                notify_fact({"call_id": call_id, "status": "handoff_requested", **safe})

    async def _run_default_action(
        self, record: dict[str, Any], execution: Mapping[str, Any], choice: str,
        action: tuple[str, dict[str, Any]],
    ) -> None:
        """Stop and settle the current call, then run one generic escape tool."""
        if self.current is not record or record["cancel_requested"]:
            return
        record["cancel_requested"] = record["settling"] = True
        call_id = record["call_id"]
        safe = self._handoff_facts(
            self._snapshot(call_id) or execution,
            reason="decision_default_tool", confirmed=False, decision=choice,
            plan=self._plan_context(
                supervision_context=record["supervision_context"],
                tool=type("Tool", (), {"name": execution.get("tool")})(), params={},
            ),
            extra={"default_choice": choice},
        )
        self.runtime.trace.emit(
            "call_supervision_default_requested", tool_call_id=call_id,
            choice=choice, tool=action[0], arguments=action[1], facts=safe,
        )
        settled = await self.runtime.cancel_active_execution_with_facts(
            "direct_call:decision_default_tool", cancel_reflex=False,
            invalidate_generation=not bool(record["supervision_context"].get("is_plan")),
        )
        default_result: Any = None
        registry = getattr(self.runtime, "registry", None)
        try:
            default_tool = registry.get(action[0]) if registry is not None else None
            if default_tool is None:
                raise LookupError(action[0])
            from minebot.brain.registry import execute_tool
            default_id = f"reflex-default-{uuid4()}"
            default_result = await self.runtime.run_tool(
                default_tool, execute_tool, default_tool, action[1],
                getattr(self.runtime, "weld_context", None),
                tool_call_id=default_id,
                claim_holder="agent_call",
                supervision_context={
                    "is_reflex_action": True,
                    "_skip_supervision": True,
                    "intent": _DEFAULT_ACTION_TEXT.get(choice, choice),
                },
            )
        except Exception as exc:
            self.runtime.trace.emit(
                "call_supervision_default_failed", tool_call_id=call_id,
                choice=choice, error_type=type(exc).__name__,
            )
            default_result = {"success": False, "reason": type(exc).__name__}
        handoff = {**safe, "default_action": {
            "choice": choice, "tool": action[0], "result": self._compact_result(default_result),
        }, "settlement": settled}
        self.runtime.direct_call_handoffs[call_id] = {"facts": handoff, "settlement": settled}
        self.runtime.trace.emit(
            "call_supervision_default_finished", tool_call_id=call_id,
            choice=choice, facts=handoff,
        )
        if not bool(record["supervision_context"].get("is_plan")):
            notify_fact = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(notify_fact):
                notify_fact({"call_id": call_id, "status": "default_action_completed", **handoff})


# Compatibility name for slice-1 callers.
DirectBuildSupervisor = CallSupervisor

__all__ = [
    "CallSupervisor", "DirectBuildSupervisor", "MAX_SUPERVISION_READS",
    "MAX_SUPERVISION_QUESTIONS", "MAX_IDLE_UNKNOWN_FRAMES",
]
