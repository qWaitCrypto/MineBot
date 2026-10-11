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
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from minebot.app.decision_client import DecisionClient, DecisionPriority
from minebot.contract.execution import tool_observation_scope

MAX_SUPERVISION_READS = 4
# Kept as a compatibility export. R2 no longer caps questions per call.
MAX_SUPERVISION_QUESTIONS = 0
MAX_IDLE_UNKNOWN_FRAMES = 3
MAX_QUESTION_CHARS = 16_000
FAST_COMPLETION_GRACE_S = 0.02

DEFAULT_TOOL_EXPLANATION = (
    "The call normally advances through native Body evidence and eventually "
    "returns one authoritative terminal result. Navigation, phase changes, "
    "world edits and inventory changes are facts; process liveness and a "
    "repeated sample are not."
)


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

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    @staticmethod
    def _explanation(tool: Any) -> str:
        value = getattr(tool, "supervision_explanation", None)
        if isinstance(value, str) and value.strip():
            return value.strip()[:800]
        description = getattr(tool, "description", None)
        return str(description).strip()[:800] if description else DEFAULT_TOOL_EXPLANATION

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
                        current_step = str(getattr(step, "title", "") or "")[:800]
                        break
        except Exception:
            # Missing plan state is an empty fact, not a reason to make a stop.
            pass
        native_plan = getattr(self.runtime, "native_plan", None)
        if not current_step and isinstance(native_plan, Mapping):
            for step in native_plan.get("steps", []):
                if isinstance(step, Mapping) and str(step.get("status") or "") == "in_progress":
                    current_step = str(step.get("title") or step.get("description") or "")[:800]
                    break
        return {"goal": goal, "current_step": current_step}

    def _plan_context(
        self, *, supervision_context: Mapping[str, Any] | None,
        tool: Any, params: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan = self._durable_plan_context()
        if isinstance(supervision_context, Mapping):
            for key in ("goal", "current_step", "node_description", "intent_chain", "is_plan"):
                if key in supervision_context:
                    plan[key] = supervision_context[key]
        node_description = str(plan.get("node_description") or "").strip()
        if not node_description:
            node_description = f"direct call {getattr(tool, 'name', 'unknown')}"
        intents = plan.get("intent_chain")
        if not isinstance(intents, list):
            intents = [] if not intents else [str(intents)]
        return self._bounded({
            "goal": str(plan.get("goal") or "")[:800],
            "current_step": str(plan.get("current_step") or "")[:800],
            "node_description": node_description[:800],
            "intent_chain": [str(item)[:400] for item in intents[:8]],
            "is_plan": bool(plan.get("is_plan", False)),
        })

    @staticmethod
    def _criteria(plan: Mapping[str, Any]) -> dict[str, str]:
        node = str(plan.get("node_description") or "the current call")
        goal = str(plan.get("goal") or "the Agent goal")
        step = str(plan.get("current_step") or "the current plan step is unavailable")
        chain = " → ".join(str(item) for item in (plan.get("intent_chain") or [])) or "no parent intent is available"
        return {
            "continue": (
                f"Choose continue when the facts show real Body progress or a phase change consistent with "
                f"node '{node}', the intent chain '{chain}', and plan step '{step}' toward goal '{goal}'."
            ),
            "inspect": (
                f"Choose inspect when the facts for node '{node}' and intent '{chain}' are insufficient to tell "
                f"whether the requested amount is enough, the step is achieved, or the work still matches '{goal}'."
            ),
            "step_done": (
                f"Choose step_done only when the supplied facts establish that node '{node}' has achieved its "
                f"description or the requested amount is already enough for plan step '{step}' and goal '{goal}'."
            ),
            "call_agent": (
                f"Choose call_agent when the facts show the work is off the intent '{chain}' or otherwise cannot "
                f"be resolved by continue, inspect, or step_done for node '{node}'."
            ),
        }

    def _question(
        self, call_id: str, tool: Any, params: Mapping[str, Any],
        execution: Mapping[str, Any], *, idle_s: float, purpose: str,
        supervision_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        plan = self._plan_context(supervision_context=supervision_context, tool=tool, params=params)
        request: dict[str, Any] = {
            "state": {
                "protocol": "minebot-call-supervision/3",
                "tool": {
                    "name": getattr(tool, "name", "unknown"),
                    "arguments": self._bounded(params),
                    "normal_work": self._explanation(tool),
                },
                "plan": plan,
                "execution": self._bounded(execution),
                "no_explainable_progress_s": round(max(0.0, idle_s), 2),
                "evidence_scope": (
                    "Only the bounded execution snapshot, plan facts, and registered normal-work explanation "
                    "are known. Omitted facts are unknown; process liveness and repeated samples are not progress."
                ),
            },
            "questions": {"q": {
                "type": "choice",
                "instructions": "Choose exactly one finite option using only the supplied facts.",
                "criteria": self._criteria(plan),
            }},
        }
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) > MAX_QUESTION_CHARS:
            request["state"]["execution"] = {
                "state": execution.get("state"), "phase": execution.get("phase"),
                "fact_revision": execution.get("fact_revision"),
                "facts": "<execution facts bounded>",
            }
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)
        request["state"]["request_chars"] = len(encoded)
        return request

    async def judge(
        self, call_id: str, tool: Any, params: Mapping[str, Any],
        execution: Mapping[str, Any], idle_s: float, *,
        purpose: str = "running_frame",
        supervision_context: Mapping[str, Any] | None = None,
    ) -> str:
        request = self._question(
            call_id, tool, params, execution, idle_s=idle_s,
            purpose=purpose, supervision_context=supervision_context,
        )
        tool_name = getattr(tool, "name", "unknown")
        request_chars = int(request["state"].get("request_chars", 0))
        fact_revision = execution.get("fact_revision")
        self.runtime.trace.emit(
            "call_supervision_request", tool_call_id=call_id, tool=tool_name,
            purpose=purpose, idle_s=round(idle_s, 3), fact_revision=fact_revision,
            request_chars=request_chars, request=request,
        )
        self.runtime.trace.emit(
            "direct_supervision_request", tool_call_id=call_id,
            regression_format="minebot-semantic-regression/1", request=request,
        )
        started = time.monotonic()
        raw: dict[str, Any] | None = None
        try:
            answer = await self._client.ask(request, priority=DecisionPriority.SUPERVISION)
            raw = answer.raw
            choice = answer.choice if answer.choice in self._criteria(request["state"]["plan"]) else "unknown"
        except Exception:
            choice = "unknown"
        elapsed = time.monotonic() - started
        self.runtime.trace.emit(
            "call_supervision_result", tool_call_id=call_id, tool=tool_name,
            purpose=purpose, response=raw, choice=choice,
            unknown=choice == "unknown", elapsed_s=elapsed,
            decision_ms=round(elapsed * 1000.0, 3), fact_revision=fact_revision,
        )
        self.runtime.trace.emit(
            "direct_supervision_result", tool_call_id=call_id, response=raw,
            choice=choice, elapsed_s=elapsed,
        )
        return choice

    async def _judge_frame(
        self, call_id: str, tool: Any, params: Mapping[str, Any],
        execution: Mapping[str, Any], idle_s: float, *, purpose: str,
        supervision_context: Mapping[str, Any] | None,
    ) -> str:
        """Call the overridable judge while accepting slice-1 test seams."""
        signature = inspect.signature(self.judge)
        accepts_context = (
            "purpose" in signature.parameters
            or "supervision_context" in signature.parameters
            or any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
        )
        if accepts_context:
            return await self.judge(
                call_id, tool, params, execution, idle_s,
                purpose=purpose, supervision_context=supervision_context,
            )
        return await self.judge(call_id, tool, params, execution, idle_s)

    async def _bounded_read(self, record: dict[str, Any], tool: Any,
                            params: Mapping[str, Any]) -> dict[str, Any]:
        reader = getattr(tool, "supervision_reader", None)
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
        if snapshots is not None:
            snapshots.enrich(record["call_id"], facts, source=source)
        record["reads"] += 1
        self.runtime.trace.emit(
            "call_supervision_read", tool_call_id=record["call_id"],
            tool=getattr(tool, "name", "unknown"), read_number=record["reads"],
            facts=self._bounded(facts),
        )
        return facts

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
        return CallSupervisor._bounded(safe)

    @staticmethod
    def _logical_step_done(result: Any) -> Any:
        """Turn an already-settled Decision completion into a node success."""
        if not isinstance(result, dict):
            return result
        result = dict(result)
        metrics = dict(result.get("metrics") or {})
        metrics["supervision_step_done"] = True
        result["metrics"] = metrics
        result["success"] = True
        result["reason"] = "decision_step_done"
        result["canRetry"] = False
        return result

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
            "started_at": time.monotonic(),
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
            if record["settling"] and monitor is not None:
                await monitor
            if record.get("decision") == "step_done":
                result = self._logical_step_done(result)
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
            self.runtime.trace.emit(
                "direct_supervision_finished", tool_call_id=call_id,
                reads=record["reads"], questions=record["questions"],
            )
            if self.current is record:
                self.current = None

    async def _monitor(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
        wake: asyncio.Event, worker: asyncio.Task[Any],
    ) -> None:
        last_revision = -1
        decision_task: asyncio.Task[str] | None = None
        pending: tuple[dict[str, Any], float, str] | None = None
        decision_revision = -1
        while not record["cancel_requested"]:
            if worker.done() and decision_task is None:
                return
            wake_wait = asyncio.create_task(wake.wait())
            wait_set: set[asyncio.Future[Any]] = {wake_wait}
            if decision_task is not None:
                wait_set.add(decision_task)
            if not worker.done():
                wait_set.add(worker)
            done, pending_tasks = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            finished_decision = decision_task if decision_task is not None and decision_task in done else None
            if wake_wait in done:
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
                        if decision_task is not None and not decision_task.done():
                            # There is no queue: only the latest frame survives.
                            pending = frame
                        elif decision_task is None and execution.get("state") not in {"succeeded", "failed", "cancelled", "unknown"}:
                            decision_revision = revision
                            decision_task = asyncio.create_task(self._judge_frame(
                                record["call_id"], tool, params, execution, idle_s,
                                purpose="running_frame",
                                supervision_context=record["supervision_context"],
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
                    wake.set()
                elif choice == "inspect":
                    self.runtime.trace.emit(
                        "call_supervision_limit", tool_call_id=record["call_id"],
                        tool=getattr(tool, "name", "unknown"), kind="reads", limit=self.max_reads,
                    )
                elif choice in {"step_done", "call_agent"}:
                    record["decision"] = choice
                    plan = self._plan_context(
                        supervision_context=record["supervision_context"],
                        tool=tool, params=params,
                    )
                    reason = "decision_step_done" if choice == "step_done" else "decision_call_agent"
                    await self._handoff(
                        record, execution,
                        {"reason": reason, "confirmed": choice == "step_done"},
                        decision=choice, plan=plan,
                    )
                    return
                if pending is not None:
                    frame = pending
                    pending = None
                    if int(frame[0].get("fact_revision") or -1) > decision_revision:
                        decision_revision = int(frame[0].get("fact_revision") or -1)
                        decision_task = asyncio.create_task(self._judge_frame(
                            record["call_id"], tool, params, frame[0], frame[1],
                            purpose=frame[2], supervision_context=record["supervision_context"],
                        ))
                        record["decision_task"] = decision_task
            if worker in done and decision_task is None:
                return
            if wake_wait not in done and decision_task is None and worker.done():
                return
            if wake_wait not in done:
                wake_wait.cancel()
                await asyncio.gather(wake_wait, return_exceptions=True)

    async def supervise_idle(
        self, tool: Any, *, call_id: str, plan_context: Mapping[str, Any] | None = None,
        duration_s: float = 2.0, frame_interval_s: float = 0.2,
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
        while time.monotonic() - started < duration_s:
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
                purpose="idle_frame", supervision_context=plan_context,
            )
            counts["questions"] += 1
            if choice == "unknown":
                counts["unknown"] += 1
                unknown_same_position += 1
                if unknown_same_position >= self.idle_unknown_limit:
                    counts["agent"] += 1
                    facts = self._handoff_facts(
                        execution, reason="idle_unknown_limit", confirmed=False,
                        decision="unknown", plan=plan_context,
                    )
                    self.runtime.trace.emit(
                        "call_supervision_idle_agent", tool_call_id=call_id,
                        reason="same_position_unknown_limit", limit=self.idle_unknown_limit,
                    )
                    notify = getattr(self.runtime, "notify_reflex_fact", None)
                    if callable(notify):
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
            await asyncio.sleep(max(0.0, frame_interval_s))
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
        kernel = self.runtime.responses_kernel
        is_plan = bool(record["supervision_context"].get("is_plan"))
        started = time.monotonic()
        settled = await self.runtime.cancel_active_execution_with_facts(
            "direct_call:" + safe["reason"], cancel_reflex=False,
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
        # Only after the unified stop path has settled may the native Agent
        # channel receive the fact. A plan step_done is consumed by its tree;
        # direct step_done and call_agent are delivered as Agent facts.
        if kernel is not None and (decision != "step_done" or not is_plan):
            kernel.notify_fact({"call_id": call_id, "status": "handoff_requested", **safe})


# Compatibility name for slice-1 callers.
DirectBuildSupervisor = CallSupervisor

__all__ = [
    "CallSupervisor", "DirectBuildSupervisor", "MAX_SUPERVISION_READS",
    "MAX_SUPERVISION_QUESTIONS", "MAX_IDLE_UNKNOWN_FRAMES",
]
