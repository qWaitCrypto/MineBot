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

from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient, DecisionPriority
from minebot.body.use import _is_food_item
from minebot.app.execution_snapshot import FACT_FRAME_INTERVAL_S
from minebot.contract.execution import tool_observation_scope

MAX_SUPERVISION_READS = 4
# Kept as a compatibility export. R2 no longer caps questions per call.
MAX_SUPERVISION_QUESTIONS = 0
MAX_IDLE_UNKNOWN_FRAMES = 3
MAX_QUESTION_CHARS = 16_000
FAST_COMPLETION_GRACE_S = 0.02
# What each default tool is for, recorded as the intent of the call it starts.
_DEFAULT_ACTION_TEXT = {
    "surface": "Swim up to air.",
    "retreat_safe": "Go back to the last place where the Body stood safely.",
    "eat": "Eat food from the inventory.",
    "move_away": "Move away from the nearby hostile mob.",
}

format_call = rq.call_text


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
        # Health and air history belongs to the Body, not to one call.
        self._vitals = rq.VitalWindow()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _frame(self, execution: Mapping[str, Any] | None) -> dict[str, Any]:
        """Add what health and air were a few seconds ago to this frame's facts."""
        if not execution:
            return {}
        facts = dict(execution.get("facts") or {})
        facts.update(self._vitals.add(time.monotonic(), facts))
        inventory = facts.get("inventory", facts.get("inventory_counts"))
        if "food_item" not in facts and isinstance(inventory, Mapping):
            # Which items are food is the Body's knowledge, not the reflex layer's.
            food = next((str(item) for item, count in inventory.items()
                         if isinstance(count, int) and count > 0 and _is_food_item(str(item))), None)
            if food:
                facts["food_item"] = food
        return {**execution, "facts": facts}

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
        state = rq.frame_state(
            goal=str(plan.get("goal") or ""), intent=intent, tool=tool_name,
            params=call_params if isinstance(call_params, Mapping) else {}, facts=facts,
            running=running, phase=execution.get("phase"), elapsed_s=execution.get("elapsed_s"),
            progress_signal=execution.get("progress_signal"),
            since_progress_s=execution.get("since_progress_s"),
            result=result if isinstance(result, Mapping) else None, earlier_attempts=earlier,
        )
        if options is None:
            options = rq.frame_options(
                tool=tool_name, params=call_params if isinstance(call_params, Mapping) else {},
                intent=intent, facts=facts, running=running,
                result=result if isinstance(result, Mapping) else None,
            )
        request: dict[str, Any] = {"state": state, "questions": rq.frame_questions(options)}
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
    ) -> str:
        request = self._question(
            call_id, tool, params, execution, idle_s=idle_s,
            purpose=purpose, supervision_context=supervision_context, options=options,
            decision_context=decision_context,
        )
        tool_name = getattr(tool, "name", "unknown")
        request_chars_before = int(request.pop("_request_chars_before", 0))
        request_chars = int(request.pop("_request_chars", request_chars_before))
        fact_revision = execution.get("fact_revision")
        self.runtime.trace.emit(
            "call_supervision_request", tool_call_id=call_id, tool=tool_name,
            purpose=purpose, idle_s=round(idle_s, 3), fact_revision=fact_revision,
            loop_state=getattr(self, "loop_state", None),
            request_chars_before=request_chars_before, request_chars=request_chars,
            request=request,
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
        )
        # A protective call is already the selected response to danger.  It
        # must be allowed to run or finish, never start the same protective
        # action again from its own supervision frame.
        reflex_choice = context.get("reflex_action_choice")
        if isinstance(reflex_choice, str):
            options.pop(reflex_choice, None)
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
            if not (isinstance(pos, (list, tuple)) and len(pos) == 3):
                near = rq.hostiles(facts)
                pos = near[0]["pos"] if near else None
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
                grace = min(FAST_COMPLETION_GRACE_S, max(0.0, self.suspicion_s))
                if grace:
                    done, _ = await asyncio.wait({worker}, timeout=grace)
                    if not done:
                        monitor = asyncio.create_task(self._monitor(record, tool, params, wake, worker))
                        record["monitor_task"] = monitor
                else:
                    monitor = asyncio.create_task(self._monitor(record, tool, params, wake, worker))
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
                if monitor is not None:
                    await monitor
                    monitor = None
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
                record["cancel_requested"] = record["settling"] = False
                record["initial_reader_done"] = record["reader_new_fact"] = False
                record.pop("decision", None)
                self.runtime.direct_call_handoffs.pop(call_id, None)
                if snapshots is not None:
                    snapshots.begin(call_id, record["tool"], record["params"])
                wake.clear()
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
                self.current = previous_current

    async def _decide_result(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any], result: Any,
    ) -> str:
        execution = self._snapshot(record["call_id"])
        if not execution:
            execution = {"tool": getattr(tool, "name", "unknown"), "facts": {}}
        payload = result if isinstance(result, Mapping) else getattr(result, "result", result)
        if not isinstance(payload, Mapping):
            payload = {"result": str(payload)[:500]}
        execution = self._frame({**execution, "state": "succeeded" if payload.get("success") is True else "failed",
                                 "result": dict(payload)})
        options = self._options_for(tool, record, execution=execution)
        body = dict(execution.get("facts") or {})
        history = record.setdefault("history", [])
        history.append({"result": self._compact_result(payload),
                        "body": {"position": body.get("position"), "inventory": body.get("inventory")}})
        record["history_total"] = int(record.get("history_total") or 0) + 1
        del history[:-5]
        context = {
            "tool_result": dict(payload),
            "tool_arguments": dict(params),
            "body_state": body,
            "execution": execution,
            "reads": [],
            "history": list(history),
            "history_earlier": record["history_total"] - len(history),
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
        if choice == "repeat":
            return "repeat"
        choice = {"next_step": "step_done"}.get(choice, choice)
        if choice in {"step_done", "call_agent"}:
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
        remember = getattr(self, "record_experience", None)
        if callable(remember):
            remember(
                f"{safe.get('what', {}).get('tool', execution.get('tool'))} was stopped by "
                f"Decision ({safe.get('decision', {}).get('choice') or 'unknown'}); "
                f"the Body is at {safe.get('body', {}).get('position')}."
            )
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
            # The earlier work may be resumed, so its owner stays valid.
            invalidate_generation=False,
        )
        default_result: Any = None
        default_id = f"reflex-default-{uuid4()}"
        registry = getattr(self.runtime, "registry", None)
        try:
            default_tool = registry.get(action[0]) if registry is not None else None
            if default_tool is None:
                raise LookupError(action[0])
            from minebot.brain.registry import execute_tool
            default_result = await self.runtime.run_tool(
                default_tool, execute_tool, default_tool, action[1],
                getattr(self.runtime, "weld_context", None),
                tool_call_id=default_id,
                claim_holder="threat",
                supervision_context={
                    "is_reflex_action": True,
                    "reflex_action_choice": choice,
                    "intent": _DEFAULT_ACTION_TEXT.get(choice, choice),
                },
            )
        except Exception as exc:
            self.runtime.trace.emit(
                "call_supervision_default_failed", tool_call_id=call_id,
                choice=choice, error_type=type(exc).__name__,
            )
            default_result = {"success": False, "reason": type(exc).__name__}
        after = await self._after_default_action(record, action, default_result, default_id)
        handoff = {**safe, "default_action": {
            "choice": choice, "tool": action[0], "result": self._compact_result(default_result),
            "then": after,
        }, "settlement": settled}
        # The plan runtime reads this choice: "repeat" re-enters the stopped node.
        handoff["decision"] = {**dict(handoff.get("decision") or {}),
                               "choice": "repeat" if after == "resume" else "call_agent"}
        self.runtime.direct_call_handoffs[call_id] = {"facts": handoff, "settlement": settled}
        self.runtime.trace.emit(
            "call_supervision_default_finished", tool_call_id=call_id,
            choice=choice, facts=handoff,
        )
        if after == "resume":
            # Back to the earlier work; the Agent is not woken for this.
            record["resume_original"] = True
        elif not bool(record["supervision_context"].get("is_plan")):
            notify_fact = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(notify_fact):
                notify_fact({"call_id": call_id, "status": "default_action_completed", **handoff})

    async def _after_default_action(
        self, record: dict[str, Any], action: tuple[str, dict[str, Any]],
        default_result: Any, default_id: str,
    ) -> str:
        """Ask what follows a protective action: go back, do it again, or the Agent."""
        context = record["supervision_context"]
        intent = str(context.get("intent") or context.get("node_description") or "")
        registry = getattr(self.runtime, "registry", None)
        default_tool = registry.get(action[0]) if registry is not None else None
        history: list[dict[str, Any]] = []
        total = 0
        while default_tool is not None:
            payload = default_result if isinstance(default_result, Mapping) else getattr(default_result, "result", default_result)
            if not isinstance(payload, Mapping):
                payload = {"success": False, "reason": str(payload)[:200]}
            state = getattr(self.runtime, "last_known_body_state", None)
            facts = dict(state) if isinstance(state, Mapping) else {}
            execution = self._frame({"tool": action[0], "state": "succeeded" if payload.get("success") is True else "failed",
                                     "facts": facts, "result": dict(payload)})
            history.append({"result": self._compact_result(payload),
                            "body": {"position": facts.get("position", facts.get("pos")), "inventory": facts.get("inventory")}})
            total += 1
            del history[:-5]
            options = rq.after_default_options(
                original_call=rq.call_text(record["tool"], record["params"]), intent=intent,
                default_call=rq.call_text(action[0], action[1]), result=payload,
                facts=execution["facts"],
            )
            try:
                choice = await asyncio.wait_for(self._judge_frame(
                    record["call_id"], default_tool, action[1], execution, 0.0,
                    purpose="default_result_frame", supervision_context=context, options=options,
                    decision_context={"tool_result": dict(payload), "tool_arguments": dict(action[1]),
                                      "body_state": execution["facts"], "history": list(history),
                                      "history_earlier": total - len(history)},
                ), timeout=2.2)
            except asyncio.TimeoutError:
                choice = "unknown"
            record["questions"] += 1
            if choice != "repeat":
                return "resume" if choice == "resume" else "call_agent"
            try:
                from minebot.brain.registry import execute_tool
                default_result = await self.runtime.run_tool(
                    default_tool, execute_tool, default_tool, action[1],
                    getattr(self.runtime, "weld_context", None),
                    tool_call_id=f"{default_id}-{total}", claim_holder="threat",
                    supervision_context={"is_reflex_action": True,
                                         "reflex_action_choice": choice,
                                         "intent": _DEFAULT_ACTION_TEXT.get(action[0], action[0])},
                )
            except Exception as exc:
                default_result = {"success": False, "reason": type(exc).__name__, "canRetry": False}
        return "call_agent"


# Compatibility name for slice-1 callers.
DirectBuildSupervisor = CallSupervisor

__all__ = [
    "CallSupervisor", "DirectBuildSupervisor", "MAX_SUPERVISION_READS",
    "MAX_SUPERVISION_QUESTIONS", "MAX_IDLE_UNKNOWN_FRAMES",
]
