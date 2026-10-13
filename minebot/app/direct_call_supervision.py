"""Default Decision supervision for every mutating Body call.

The supervisor compiles bounded facts from :mod:`execution_snapshot`, asks one
finite-choice Decision question for each new fact frame, and leaves admission,
stopping and settlement to the existing runtime entry points. Direct calls and
plan call nodes use this same path.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import time
from uuid import uuid4
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient, DecisionPriority
from minebot.body.use import _is_food_item
from minebot.app.execution_snapshot import FACT_FRAME_INTERVAL_S
from minebot.contract.execution import tool_observation_scope

MAX_SUPERVISION_READS = 4
# Kept as a compatibility export. R2 no longer caps questions per call.
MAX_QUESTION_CHARS = 16_000
FAST_COMPLETION_GRACE_S = 0.02
# What each default tool is for, recorded as the intent of the call it starts.
_DEFAULT_ACTION_TEXT = rq.DEFAULT_ACTION_INTENTS

format_call = rq.call_text


class CallSupervisor:
    """Observe all mutating calls through one frame-by-frame Decision loop."""

    def __init__(
        self,
        runtime: Any,
        *,
        decision_url: str | None = None,
        max_reads: int = MAX_SUPERVISION_READS,
    ) -> None:
        self.runtime = runtime
        self.decision_url = decision_url or os.environ.get(
            "MINEBOT_DECISION_URL", "http://117.176.14.165:10004/v1/systemone"
        )
        self.max_reads = max(0, int(max_reads))
        self._client = getattr(runtime, "decision_client", None) or DecisionClient(
            url=self.decision_url, emit=runtime.trace.emit
        )
        self.current: dict[str, Any] | None = None
        # Health and air history belongs to the Body, not to one call.
        self._vitals = rq.VitalWindow()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _start_monitor(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
        wake: asyncio.Event, worker: asyncio.Task[Any],
    ) -> asyncio.Task[Any] | None:
        """Start the Body-owned frame consumer.

        The concrete per-Body loop supplies this hook.  Keeping the call
        runner responsible only for registration and awaiting the returned
        task prevents a second supervision implementation from appearing in
        the execution path.
        """
        raise RuntimeError("a BodyLoop is required for mutating calls")

    async def _wait_monitor(self, record: Mapping[str, Any]) -> None:
        """Wait for a Body-owned monitor when a concrete loop registered one."""
        return None

    def _release_result_pending(self, call_id: str) -> None:
        """Allow a concrete Body loop to resume idle after result judgment."""
        return None

    def _frame(self, execution: Mapping[str, Any] | None) -> dict[str, Any]:
        """Add what health and air were a few seconds ago to this frame's facts."""
        if not execution:
            return {}
        facts = dict(execution.get("facts") or {})
        frame_clock = time.monotonic()
        frame_input = {"execution": copy.deepcopy({key: value for key, value in execution.items()
                                                  if key != "_frame_input"}),
                       "monotonic_s": frame_clock,
                       "vital_samples": copy.deepcopy(self._vitals._samples)}
        self._last_frame_input = frame_input
        facts.update(self._vitals.add(frame_clock, facts))
        inventory = facts.get("inventory", facts.get("inventory_counts"))
        if "food_item" not in facts and isinstance(inventory, Mapping):
            # Which items are food is the Body's knowledge, not the reflex layer's.
            food = next((str(item) for item, count in inventory.items()
                         if isinstance(count, int) and count > 0 and _is_food_item(str(item))), None)
            if food:
                facts["food_item"] = food
        return {**execution, "facts": facts, "_frame_input": frame_input}

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
    def _compact_result(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"result": str(value)[:500]}
        result = {key: value[key] for key in ("success", "reason", "canRetry", "complete", "outcome") if key in value}
        summary = value.get("summary")
        if isinstance(summary, Mapping):
            result["summary"] = CallSupervisor._bounded(dict(summary), depth=0)
        return result

    def _question(
        self, call_id: str, tool: Any, params: Mapping[str, Any],
        execution: Mapping[str, Any], *, idle_s: float, purpose: str,
        supervision_context: Mapping[str, Any] | None,
        options: Mapping[str, str] | None = None,
        decision_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write one frame as a request; all wording lives in reflex_question."""
        plan = self._plan_context(supervision_context=supervision_context, tool=tool, params=params)
        context = decision_context if isinstance(decision_context, Mapping) else {}
        tool_name = str(getattr(tool, "name", "unknown"))
        result = context.get("tool_result")
        running = not isinstance(result, Mapping)
        facts = execution.get("facts") if isinstance(execution.get("facts"), Mapping) else {}
        if not running and isinstance(context.get("body_state"), Mapping) and context["body_state"]:
            facts = context["body_state"]
        call_params = context.get("tool_arguments", params)
        intent = str(plan.get("intent") or plan.get("node_description") or "")
        history = context.get("history")
        earlier = rq.attempt_lines(
            [item for item in history if isinstance(item, Mapping)],
            int(context.get("history_earlier") or 0),
        ) if isinstance(history, list) else []
        interrupted = context.get("interrupted_history")
        interrupted_lines = rq.interruption_lines(
            [item for item in interrupted if isinstance(item, Mapping)],
            int(context.get("interrupted_history_earlier") or 0),
        ) if isinstance(interrupted, list) else []
        state = rq.frame_state(
            goal=str(plan.get("goal") or ""), intent=intent, tool=tool_name,
            params=call_params if isinstance(call_params, Mapping) else {}, facts=facts,
            running=running, phase=execution.get("phase"), elapsed_s=execution.get("elapsed_s"),
            progress_signal=execution.get("progress_signal"),
            since_progress_s=execution.get("since_progress_s"),
            result=result if isinstance(result, Mapping) else None, earlier_attempts=earlier,
            interrupted_attempts=interrupted_lines,
        )
        if options is None:
            options = rq.frame_options(
                tool=tool_name, params=call_params if isinstance(call_params, Mapping) else {},
                intent=intent, facts=facts, running=running,
                result=result if isinstance(result, Mapping) else None,
            )
        request: dict[str, Any] = {"state": state, "questions": rq.frame_questions(
            options, protecting=bool((supervision_context or {}).get("is_reflex_action")),
            acting_for_intent=running and bool(intent))}
        size = len(json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str))
        # Private values, removed before the request reaches Decision.
        request["_request_chars_before"] = size
        request["_request_chars"] = size
        return request

    async def judge(
        self, call_id: str, tool: Any, params: Mapping[str, Any],
        execution: Mapping[str, Any], idle_s: float, *,
        purpose: str = "running_frame",
        supervision_context: Mapping[str, Any] | None = None,
        options: Mapping[str, str] | None = None,
        decision_context: Mapping[str, Any] | None = None,
        question: Mapping[str, Any] | None = None,
    ) -> str:
        # Plan nodes compile their terminal facts through the existing tree
        # compiler. The Body loop judges that same request; rebuilding it here
        # would change the goal bounds, repeat wording and request metadata.
        request = dict(question) if question is not None else self._question(
            call_id, tool, params, execution, idle_s=idle_s,
            purpose=purpose, supervision_context=supervision_context, options=options,
            decision_context=decision_context,
        )
        tool_name = getattr(tool, "name", "unknown")
        compiled_chars = len(json.dumps(
            request, ensure_ascii=False, separators=(",", ":"), default=str,
        )) if question is not None else 0
        request_chars_before = int(request.pop("_request_chars_before", compiled_chars))
        request_chars = int(request.pop("_request_chars", request_chars_before))
        fact_revision = execution.get("fact_revision")
        self.runtime.trace.emit(
            "call_supervision_request", tool_call_id=call_id, tool=tool_name,
            purpose=purpose, idle_s=round(idle_s, 3), fact_revision=fact_revision,
            loop_state=getattr(self, "loop_state", None),
            request_chars_before=request_chars_before, request_chars=request_chars,
            request=request,
            frame_input=execution.get("_frame_input", {"execution": dict(execution)}),
            compiler_input={"params": dict(params), "supervision_context": dict(supervision_context or {}),
                            "decision_context": copy.deepcopy(dict(decision_context or {})),
                            "options": dict(options) if options is not None else None},
            clock={"request_monotonic_s": time.monotonic(),
                   "elapsed_s": execution.get("elapsed_s"),
                   "since_progress_s": execution.get("since_progress_s")},
        )
        started = time.monotonic()
        raw: dict[str, Any] | None = None
        try:
            choice = rq.only_option(request["questions"])
            if choice is None:
                answer = await self._client.ask(request, priority=DecisionPriority.SUPERVISION)
                raw = answer.raw
                choice = rq.frame_choice(raw, request["questions"])
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
        question: Mapping[str, Any] | None = None,
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
            compiled = {"question": question} if question is not None else {}
            return await self.judge(
                call_id, tool, params, execution, idle_s,
                purpose=purpose, supervision_context=supervision_context, options=options,
                decision_context=decision_context,
                **compiled,
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
        """The concrete things the Body may do in this frame."""
        current = execution or {}
        terminal = str(current.get("state") or "") in {"succeeded", "failed", "cancelled", "unknown"}
        context = record.get("supervision_context")
        context = context if isinstance(context, Mapping) else {}
        options = rq.frame_options(
            tool=str(record.get("tool") or getattr(tool, "name", "unknown")),
            params=record.get("params") if isinstance(record.get("params"), Mapping) else {},
            intent=str(context.get("intent") or context.get("node_description") or ""),
            facts=cls._facts_for_options(current),
            running=not terminal,
            result=current.get("result") if isinstance(current.get("result"), Mapping) else None,
            next_call=str(context.get("next_call") or ""),
            tree=supplied,
            intent_of_plan=bool(context.get("intent_of_plan")),
            interrupt=context.get("interrupt_when"),
        )
        # A protective call is already the selected response to danger.  It
        # must be allowed to run or finish, never start the same protective
        # action again from its own supervision frame.
        reflex_choice = context.get("reflex_action_choice")
        if isinstance(reflex_choice, str):
            response = cls._default_action(reflex_choice, current)
            if reflex_choice not in {"engage", "ranged"} or response is None or response[1].get("target") == record.get("params", {}).get("target"):
                options.pop(reflex_choice, None)
        if context.get("is_reflex_action") and not terminal:
            options.pop("next_step", None)
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
        if choice in {"engage", "ranged"}:
            near = rq.hostiles(facts)
            target = near[0].get("id") if near else None
            if isinstance(target, str) and target:
                if choice == "engage":
                    return "engage_entity", {"target": target}
                inventory = rq._inventory(facts) or {}
                weapon = next((item for item in ("bow", "crossbow")
                               if inventory.get(item, inventory.get("minecraft:" + item, 0)) > 0), None)
                if weapon:
                    return "ranged_attack", {"target": target, "weapon": weapon}
        if choice == "move_away":
            pos = facts.get("nearest_hostile_position")
            if not (isinstance(pos, (list, tuple)) and len(pos) == 3):
                near = rq.hostiles(facts)
                pos = near[0]["pos"] if near else None
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                return "move_away", {"danger_pos": [float(value) for value in pos], "min_distance": 8.0}
        if choice == "go_home":
            profile = facts.get("life_profile")
            home = profile.get("home") if isinstance(profile, Mapping) else None
            pos = home.get("pos") if isinstance(home, Mapping) else None
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                return "move_to", {"pos": list(pos), "radius": 2, "timeout_s": 60.0}
        if choice == "approach_player":
            players = [item for item in facts.get("nearby_entities", [])
                       if isinstance(item, Mapping) and item.get("entity_type", item.get("type")) == "player"]
            if players:
                player = players[0]
                return "move_to", {"pos": player.get("pos"), "radius": 2, "timeout_s": 60.0}
        if choice == "follow_player":
            players = [item for item in facts.get("nearby_entities", [])
                       if isinstance(item, Mapping) and item.get("entity_type", item.get("type")) == "player"]
            if players and players[0].get("id"):
                return "follow_entity", {"target": players[0]["id"], "timeout_s": 60.0}
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
        readmit: Callable[[], None] | None = None,
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
            "timeout_s": timeout_s,
            "initial_reader_done": False,
            "reader_new_fact": False,
            "history": copy.deepcopy((supervision_context or {}).get("history", [])),
            "history_total": (len((supervision_context or {}).get("history", []))
                              + int((supervision_context or {}).get("history_earlier") or 0)),
        }
        # A protective action can be launched while its parent call is still
        # settling.  Preserve that parent so the per-Body loop does not lose
        # ownership when the short reflex call returns.
        previous_current = self.current
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
            observe = getattr(self, "observe_native_action_progress", None)
            if callable(observe):
                observe(call_id, payload)
            if telemetry is not None:
                telemetry.observe(call_id, payload)
            loop.call_soon_threadsafe(receive, {**dict(payload), "call_id": call_id})

        def invoke() -> Any:
            if snapshots is not None:
                snapshots.running(call_id)
            with tool_observation_scope(call_id, notify):
                return callback(*args)

        params = args[1] if len(args) > 1 and isinstance(args[1], Mapping) else {}
        monitor: asyncio.Task[Any] | None = None
        direct = (getattr(self.runtime, "reflex_manager", None) is not None
                  and not bool(record["supervision_context"].get("is_plan"))
                  and not bool(record["supervision_context"].get("is_reflex_action")))
        try:
            while True:
                worker = asyncio.create_task(self.runtime.execution_lane.run(invoke, timeout_s=timeout_s))
                # Give very short calls a small completion race window. This
                # is scheduling protection, not a supervision gate: calls
                # that remain live are always handed to BodyLoop._monitor.
                grace = FAST_COMPLETION_GRACE_S
                if grace:
                    done, _ = await asyncio.wait({worker}, timeout=grace)
                    if not done:
                        monitor = self._start_monitor(record, tool, params, wake, worker)
                        record["monitor_task"] = monitor
                else:
                    monitor = self._start_monitor(record, tool, params, wake, worker)
                    record["monitor_task"] = monitor
                try:
                    result = await worker
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if not record["cancel_requested"] or (task is not None and task.cancelling() > 0):
                        raise
                    # Our own stop reached the tool as a cancellation. That
                    # ends this call, not its caller: the stop still has to
                    # settle and be handed on like any other stopped call.
                    result = {"success": False, "reason": "stopped_by_decision",
                              "canRetry": True, "metrics": {}}
                record["result"] = result
                if monitor is not None:
                    await monitor
                    monitor = None
                elif record.get("monitor_registered"):
                    await self._wait_monitor(record)
                # A direct call is a one-node work tree: when it ends, Decision
                # chooses between running it again, calling it done, and the
                # Agent.  Plan calls are judged by WorkPlanExecutor instead.
                again = False
                if direct and record.pop("resume_original", False):
                    again = True
                elif direct and not record["cancel_requested"]:
                    again = await self._decide_result(record, tool, params, result) == "repeat"
                if not again:
                    break
                # The unified stop released the old Body claim. A resumed
                # direct call must re-enter the same admission boundary before
                # the callback can write; it cannot reuse the old generation.
                if readmit is not None:
                    readmit()
                record["cancel_requested"] = record["settling"] = False
                record.pop("result", None)
                record["initial_reader_done"] = record["reader_new_fact"] = False
                record.pop("decision", None)
                self.runtime.direct_call_handoffs.pop(call_id, None)
                if snapshots is not None:
                    snapshots.begin(call_id, record["tool"], record["params"])
                wake.clear()
            handoff = self.runtime.direct_call_handoffs.get(call_id)
            payload = result if isinstance(result, dict) else getattr(result, "result", None)
            if handoff is not None and isinstance(payload, dict):
                if not isinstance(payload.get("metrics"), dict):
                    payload["metrics"] = {}
                payload["metrics"]["direct_call_handoff"] = handoff
            metrics = payload.get("metrics") if isinstance(payload, dict) else None
            if isinstance(metrics, dict) and metrics.get("control_unknown"):
                self.runtime.body_control.mark_unknown()
            return result
        finally:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                # A cancelled containing plan will never ask a result
                # question. Its lifecycle completion must not reserve a slot.
                record["result_abandoned"] = True
                self._release_result_pending(str(record.get("call_id") or ""))
            # Plan calls are judged by WorkPlanExecutor immediately after this
            # method returns; BodyLoop releases their result slot from
            # decide_plan_result. Direct calls have no second owner and can
            # release here.
            if not bool(record.get("supervision_context", {}).get("is_plan")):
                self._release_result_pending(str(record.get("call_id") or ""))
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
                self.current = previous_current



# Compatibility name for slice-1 callers.
DirectBuildSupervisor = CallSupervisor

__all__ = [
    "CallSupervisor", "DirectBuildSupervisor", "MAX_SUPERVISION_READS",
]
