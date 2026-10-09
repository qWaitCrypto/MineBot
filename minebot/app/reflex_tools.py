"""Agent-facing reflex management tools and activity state."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any
from uuid import uuid4

from minebot.app.decision_client import DecisionClient
from minebot.app.work_runtime import NodeRecord, WorkPlanError, WorkPlanExecutor, WorkRunResult, validate_plan
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, execute_tool
from minebot.contract import ToolResult


@dataclass
class ReflexActivity:
    activity_id: str
    plan: dict[str, Any]
    records: dict[str, NodeRecord] = field(default_factory=dict)
    status: str = "accepted"
    result: WorkRunResult | None = None
    task: asyncio.Task[Any] | None = None
    revision: int = 1
    created_at: float = field(default_factory=time.time)
    cancel_reason: str | None = None


class ReflexManager:
    """Own one plan activity and expose a same-tree management surface."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._lock = threading.RLock()
        self._activities: dict[str, ReflexActivity] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_activity_id: str | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def close(self) -> None:
        with self._lock:
            tasks = [activity.task for activity in self._activities.values()
                     if activity.task is not None and not activity.task.done()]
            for activity in self._activities.values():
                if activity.task is not None and not activity.task.done():
                    activity.status = "cancelled"
                    activity.cancel_reason = "runtime_closed"
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def request_cancel(self, reason: str, *, final_status: str = "cancelled") -> None:
        """Revoke a plan before an external/native stop begins."""
        with self._lock:
            for activity in self._activities.values():
                if activity.status not in {"accepted", "running", "waiting", "stopping"}:
                    continue
                activity.cancel_reason = reason
                activity.status = final_status
                if activity.task is not None and not activity.task.done():
                    activity.task.cancel()
                break

    def delegate(self, spec: dict[str, Any]) -> dict[str, Any]:
        plan = spec.get("plan", spec)
        try:
            validate_plan(plan)
        except WorkPlanError as exc:
            return ToolResult(False, "invalid_plan", True, metrics={"error": str(exc)}).to_payload()
        with self._lock:
            active = next((item for item in self._activities.values() if item.status in {"accepted", "running", "waiting"}), None)
            if active is not None:
                return ToolResult(False, "waiting_current", True, metrics={"activity_id": active.activity_id}).to_payload()
            activity = ReflexActivity(f"activity-{uuid4().hex}", copy.deepcopy(plan))
            self._activities[activity.activity_id] = activity
            loop = self._loop
            if loop is None:
                return ToolResult(True, "accepted", True, metrics={"activity_id": activity.activity_id, "status": "accepted"}).to_payload()
            activity.status = "running"
            activity.task = loop.create_task(self._run(activity))
        return ToolResult(True, "accepted", True, metrics={
            "activity_id": activity.activity_id,
            "status": activity.status,
            "tree": self._tree(activity),
        }).to_payload()

    async def _run(self, activity: ReflexActivity) -> None:
        # A plan submitted while a direct call owns the Body waits for the
        # native owner to settle.  The retry is local bookkeeping; it never
        # starts a second physical action or a second Decision request.
        while True:
            if activity.status in {"cancelled", "paused"}:
                return
            try:
                self.runtime.body_control.claim(activity.activity_id, holder="agent_plan")
            except RuntimeError as exc:
                reason = str(exc)
                if reason.startswith("body_busy:"):
                    activity.status = "waiting"
                    self.runtime.trace.emit(
                        "reflex_activity_waiting_current",
                        activity_id=activity.activity_id,
                        reason=reason,
                    )
                    await asyncio.sleep(0.05)
                    continue
                activity.status = "waiting"
                activity.result = WorkRunResult("waiting", self._tree(activity), {}, {"reason": reason})
                self.runtime.trace.emit(
                    "reflex_activity_waiting_current",
                    activity_id=activity.activity_id,
                    reason=reason,
                )
                return
            break
        self._active_activity_id = activity.activity_id
        executor = WorkPlanExecutor(
            activity.plan,
            call=self._call,
            state=self._state,
            decision=self.runtime.decision_client,
            choice_provider=getattr(self.runtime, "choice_provider", None),
            emit=lambda event, **payload: self.runtime.trace.emit(event, activity_id=activity.activity_id, **payload),
            records=activity.records,
        )
        try:
            while True:
                result = await executor.run()
                activity.result = result
                activity.records = executor.records
                activity.status = result.status
                if result.status != "waiting":
                    break
                self.runtime.trace.emit("reflex_activity_waiting", activity_id=activity.activity_id,
                                        tree=result.tree, facts=result.facts)
                # Keep the claim and the activity alive.  A state/event-aware
                # scheduler can later replace this short yield with a wake
                # event; the plan must not disappear after one pending answer.
                await asyncio.sleep(0.05)
                if activity.status in {"cancelled", "paused", "stopping"}:
                    return
            self.runtime.trace.emit("reflex_activity_finished", activity_id=activity.activity_id,
                                    status=result.status, tree=result.tree)
            fact = {
                "activity_id": activity.activity_id,
                "status": result.status,
                "tree": result.tree,
                "facts": result.facts,
            }
            wake = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(wake):
                wake(fact)
            elif self.runtime.responses_kernel is not None:
                self.runtime.responses_kernel.notify_fact(fact)
        except asyncio.CancelledError:
            if activity.status not in {"paused", "cancelled", "updating"}:
                activity.status = "cancelled"
            self.runtime.trace.emit("reflex_activity_cancelled", activity_id=activity.activity_id)
            raise
        except Exception as exc:
            activity.status = "blocked"
            self.runtime.trace.emit("reflex_activity_failed", activity_id=activity.activity_id,
                                    error_type=type(exc).__name__, message=str(exc)[:400])
        finally:
            if self._active_activity_id == activity.activity_id:
                self._active_activity_id = None
            if (
                activity.status not in {"stopping", "cancelled", "paused", "updating"}
                and self.runtime.body_control.owns(activity.activity_id)
            ):
                self.runtime.body_control.settle(claim_id=activity.activity_id)

    async def _state(self) -> dict[str, Any]:
        if self.runtime.last_known_body_state:
            return copy.deepcopy(self.runtime.last_known_body_state)
        try:
            state = await self.runtime._read_lane.run(self.runtime.body.get_state)
            if isinstance(state, dict):
                return copy.deepcopy(state)
            if is_dataclass(state):
                return copy.deepcopy(asdict(state))
            values = getattr(state, "__dict__", None)
            return copy.deepcopy(values) if isinstance(values, dict) else {}
        except Exception:
            return {}

    async def _call(self, tool_name: str, arguments: dict[str, Any], *, call_id: str, timeout_s: float | None) -> dict[str, Any]:
        tool = self.runtime.registry.get(tool_name)
        return await self.runtime.run_tool(
            tool,
            execute_tool,
            tool,
            arguments,
            self.runtime.weld_context,
            timeout_s=timeout_s or tool.sidecar.timeout_s,
            tool_call_id=f"plan-{call_id}",
            claim_holder="agent_plan",
            body_claim_id=self._active_activity_id,
            retain_claim=self._active_activity_id is not None,
        )

    def read(self, activity_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            activity = self._select(activity_id)
            if activity is None:
                return ToolResult(False, "no_reflex", True).to_payload()
            return ToolResult(True, "ok", True, metrics={
                "activity_id": activity.activity_id,
                "status": activity.status,
                "tree": self._tree(activity),
                "body_control": self.runtime.body_control.snapshot().to_payload(),
            }).to_payload()

    def control(self, params: dict[str, Any]) -> dict[str, Any]:
        operation = str(params.get("operation", ""))
        activity_id = params.get("activity_id")
        with self._lock:
            activity = self._select(str(activity_id) if activity_id else None)
            if operation == "take_control":
                if activity is None:
                    loop = self._loop
                    stop = getattr(self.runtime, "cancel_active_execution_with_facts", None)
                    if loop is None or not callable(stop):
                        return ToolResult(False, "event_loop_unavailable", True).to_payload()
                    async def stop_direct() -> Any:
                        try:
                            return await stop("reflex_take_control", cancel_reflex=False)
                        except TypeError:
                            return await stop("reflex_take_control")
                    coroutine = stop_direct()
                    try:
                        running = asyncio.get_running_loop()
                    except RuntimeError:
                        running = None
                    if running is loop:
                        task = loop.create_task(coroutine)
                        return ToolResult(True, "take_control_requested", True,
                                          metrics={"settlement_pending": True, "task": id(task)}).to_payload()
                    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                    return ToolResult(True, "take_control_requested", True,
                                      metrics={"settlement": future.result(timeout=35)}).to_payload()
                return self._schedule_stop(activity, "reflex_take_control", "cancelled")
            if activity is None:
                return ToolResult(False, "no_reflex", True).to_payload()
            if operation == "cancel":
                return self._schedule_stop(activity, "reflex_cancel", "cancelled")
            if operation == "pause":
                return self._schedule_stop(activity, "reflex_pause", "paused")
            if operation == "resume":
                if activity.status == "paused" and self._loop is not None:
                    activity.cancel_reason = None
                    activity.status = "running"
                    activity.task = self._loop.create_task(self._run(activity))
                return ToolResult(True, "resumed", True, metrics={"activity_id": activity.activity_id, "status": activity.status}).to_payload()
            if operation == "update":
                spec = params.get("spec")
                try:
                    validate_plan(spec)
                except (WorkPlanError, TypeError) as exc:
                    return ToolResult(False, "invalid_plan", True, metrics={"error": str(exc)}).to_payload()
                if self._loop is None:
                    activity.plan = copy.deepcopy(spec)
                    activity.revision += 1
                    activity.status = "accepted"
                    return ToolResult(True, "updated", True, metrics={"activity_id": activity.activity_id, "tree": self._tree(activity)}).to_payload()

                async def replace_plan() -> None:
                    old_task = activity.task
                    activity.cancel_reason = "update"
                    activity.status = "stopping"
                    if old_task is not None and not old_task.done():
                        old_task.cancel()
                    stop = getattr(self.runtime, "cancel_active_execution_with_facts", None)
                    if callable(stop):
                        facts = await stop("reflex_update", cancel_reflex=False)
                        if not facts.get("settled", False):
                            activity.status = "blocked"
                            return
                    if old_task is not None and old_task is not asyncio.current_task():
                        await asyncio.gather(old_task, return_exceptions=True)
                    activity.plan = copy.deepcopy(spec)
                    activity.revision += 1
                    activity.cancel_reason = None
                    activity.status = "running"
                    activity.task = self._loop.create_task(self._run(activity))

                self._loop.create_task(replace_plan())
                return ToolResult(True, "updated", True, metrics={"activity_id": activity.activity_id, "status": "updating"}).to_payload()
            return ToolResult(False, "unknown_control_operation", True).to_payload()

    def _schedule_stop(self, activity: ReflexActivity, reason: str, final_status: str) -> dict[str, Any]:
        activity.status = "stopping"
        activity.cancel_reason = reason

        async def stop_activity() -> dict[str, Any]:
            task = activity.task
            if task is not None and not task.done():
                task.cancel()
            stop = getattr(self.runtime, "cancel_active_execution_with_facts", None)
            facts = {"settled": True, "native_stopped": True}
            if callable(stop):
                try:
                    facts = await stop(reason, cancel_reflex=False)
                except TypeError:
                    facts = await stop(reason)
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            if facts.get("settled", False):
                activity.status = final_status
            else:
                activity.status = "blocked"
            self.runtime.trace.emit(
                "reflex_activity_stopped",
                activity_id=activity.activity_id,
                status=activity.status,
                reason=reason,
                settlement=facts,
            )
            return facts

        loop = self._loop
        if loop is None:
            return ToolResult(False, "event_loop_unavailable", True).to_payload()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            task = loop.create_task(stop_activity())
            return ToolResult(True, "stop_requested", True, metrics={
                "activity_id": activity.activity_id,
                "status": "stopping",
                "settlement_pending": True,
                "task": id(task),
            }).to_payload()
        future = asyncio.run_coroutine_threadsafe(stop_activity(), loop)
        try:
            facts = future.result(timeout=35)
        except Exception as exc:
            return ToolResult(False, "stop_unconfirmed", True, metrics={
                "activity_id": activity.activity_id,
                "error_type": type(exc).__name__,
            }).to_payload()
        return ToolResult(True, final_status if facts.get("settled") else "stop_unconfirmed", True,
                          metrics={"activity_id": activity.activity_id, "settlement": facts,
                                   "tree": self._tree(activity)}).to_payload()

    def _select(self, activity_id: str | None) -> ReflexActivity | None:
        if activity_id:
            return self._activities.get(activity_id)
        return next(reversed(self._activities.values()), None) if self._activities else None

    def _tree(self, activity: ReflexActivity) -> dict[str, Any]:
        if activity.result is not None:
            return activity.result.tree
        try:
            executor = WorkPlanExecutor(activity.plan, call=self._call, state=self._state,
                                        decision=self.runtime.decision_client,
                                        choice_provider=getattr(self.runtime, "choice_provider", None),
                                        records=activity.records)
            return executor.annotated_tree()
        except Exception:
            return copy.deepcopy(activity.plan)


def register_reflex_tools(registry: ToolRegistry, manager: ReflexManager) -> None:
    registry.register(RegisteredTool(
        "delegate_work", "Submit one minebot-work/0.2 plan for reflex execution.",
        {"type": "object", "properties": {"plan": {"type": "object"}}, "required": ["plan"], "additionalProperties": False},
        manager.delegate,
        ToolSidecar("reflex_delegate", mutating=False, source="agent.reflex", tool_type="plan", permission="manage_reflex"),
    ))
    registry.register(RegisteredTool(
        "control_reflex", "Pause, resume, cancel, revise, or take control of a reflex activity.",
        {"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["update", "pause", "resume", "cancel", "take_control"]},
            "activity_id": {"type": "string"}, "spec": {"type": "object"},
        }, "required": ["operation"], "additionalProperties": False},
        manager.control,
        ToolSidecar("reflex_control", mutating=False, source="agent.reflex", tool_type="plan", permission="manage_reflex"),
    ))
    registry.register(RegisteredTool(
        "read_reflex", "Read the annotated plan tree, real results, and current Body control.",
        {"type": "object", "properties": {"activity_id": {"type": "string"}}, "additionalProperties": False},
        lambda params: manager.read(params.get("activity_id")),
        ToolSidecar("reflex_read", mutating=False, source="agent.reflex", tool_type="plan", permission="observe_reflex"),
    ))


__all__ = ["ReflexActivity", "ReflexManager", "register_reflex_tools"]
