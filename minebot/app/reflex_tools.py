"""Agent-facing reflex management tools and activity state."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any
from uuid import uuid4

from minebot.app.decision_client import DecisionClient
from minebot.app.projection import answer_value
from minebot.app.work_runtime import NodeRecord, WorkPlanError, WorkPlanExecutor, WorkRunResult, validate_plan
from minebot.brain.progress import material_progress_evidence
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, execute_tool
from minebot.contract import ToolResult


def _execute_plan_call(tool: Any, arguments: Any, context: Any) -> Any:
    """Run one plan call without feeding the Agent's progress counters.

    Those counters stop an Agent that repeats itself.  A plan's repeats are
    Decision's choice; the counters hear of the plan once, when it ends.
    """
    authority = getattr(context, "authority", None)
    if authority is None or not callable(getattr(authority, "capture_steps", None)):
        return execute_tool(tool, arguments, context)
    with authority.capture_steps():
        return execute_tool(tool, arguments, context)


@dataclass
class ReflexActivity:
    activity_id: str
    plan: dict[str, Any]
    records: dict[str, NodeRecord] = field(default_factory=dict)
    status: str = "accepted"
    result: WorkRunResult | None = None
    # ``task`` may be an asyncio Task (same-loop caller) or a
    # concurrent.futures.Future (tool invoked from the read lane).
    task: Any = None
    revision: int = 1
    created_at: float = field(default_factory=time.time)
    cancel_reason: str | None = None
    # Projected call results already shown to the Agent, so repeated reads of
    # one tree do not store the same original again.
    shown: dict[str, dict[str, Any]] = field(default_factory=dict)


class ReflexManager:
    """Own one plan activity and expose a same-tree management surface."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._lock = threading.RLock()
        self._activities: dict[str, ReflexActivity] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_activity_id: str | None = None
        authority = getattr(runtime, "authority", None)
        if authority is not None and callable(getattr(authority, "add_generation_listener", None)):
            authority.add_generation_listener(self._on_generation_invalidated)

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
                    task = activity.task
                    loop = self._loop
                    if loop is not None:
                        try:
                            running = asyncio.get_running_loop()
                        except RuntimeError:
                            running = None
                        if running is loop:
                            task.cancel()
                        else:
                            loop.call_soon_threadsafe(task.cancel)
                    else:
                        task.cancel()
                break

    def _on_generation_invalidated(self, reason: str, _generation: int) -> None:
        """External invalidation revokes an active plan immediately."""
        self.request_cancel(f"generation_invalidated:{reason}")

    def _notify_activity(self, activity: ReflexActivity) -> None:
        """Publish one terminal activity fact to whichever Agent channel exists."""
        result = activity.result
        fact = {
            "activity_id": activity.activity_id,
            "status": activity.status,
            "tree": self._shown_tree(activity),
            "facts": self._shown_facts(activity),
        }
        wake = getattr(self.runtime, "notify_reflex_fact", None)
        if callable(wake):
            wake(fact)
        elif self.runtime.responses_kernel is not None:
            self.runtime.responses_kernel.notify_fact(fact)

    def _submit(self, coroutine):
        loop = self._loop
        if loop is None:
            return None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return loop.create_task(coroutine)
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    async def _await_task(self, task: Any) -> None:
        if task is None or task is asyncio.current_task():
            return
        if isinstance(task, asyncio.Future):
            await task
            return
        await asyncio.wrap_future(task)

    def delegate(self, spec: dict[str, Any]) -> dict[str, Any]:
        plan = spec.get("plan", spec)
        root = plan.get("work") if isinstance(plan, dict) else None
        if not isinstance(root, dict) or not str(root.get("intent") or "").strip():
            return ToolResult(
                False, "missing_intent", False,
                next_suggestion="Add an intent to the plan's outermost work node before delegating it.",
                metrics={"missing": "work.intent"},
            ).to_payload()
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
            activity.task = self._submit(self._run(activity))
        return ToolResult(True, "accepted", True, metrics={
            "activity_id": activity.activity_id,
            "status": activity.status,
            "tree": self._shown_tree(activity),
        }).to_payload()

    async def _progress_state(self) -> Any:
        read = getattr(self.runtime, "read_progress_state", None)
        return await read() if callable(read) else None

    async def _note_plan_outcome(self, activity: ReflexActivity, status: str, before: Any) -> None:
        """Tell the Agent's progress counters about the plan once, as one step.

        The plan's own calls are the reflex layer's attempts and are judged by
        Decision.  For the Agent the step is the delegation: it worked when
        the plan completed or the Body ended up holding more than before.
        """
        authority = getattr(self.runtime, "authority", None)
        after = await self._progress_state()
        if authority is None or after is None:
            return
        gained = material_progress_evidence(before, after, successful_body_action=False)
        work = json.dumps(activity.plan.get("work"), sort_keys=True, default=str, ensure_ascii=False)
        authority.note_step(
            ("delegate_work", hashlib.sha256(work.encode()).hexdigest()[:16]),
            status == "completed" or bool(gained), authority.fingerprint(after),
        )
        self.runtime.trace.emit(
            "reflex_activity_progress_noted", activity_id=activity.activity_id,
            status=status, material_evidence=list(gained),
        )

    async def _run(self, activity: ReflexActivity) -> None:
        run_revision = activity.revision
        self._active_activity_id = activity.activity_id
        state_before = await self._progress_state()

        async def dispatch(tool_name, arguments, *, call_id, timeout_s):
            return await self._call(
                tool_name, arguments, call_id=call_id, timeout_s=timeout_s,
                supervision_context=executor.call_context(call_id),
            )

        executor = WorkPlanExecutor(
            activity.plan,
            call=dispatch,
            state=self._state,
            decision=self.runtime.decision_client,
            choice_provider=getattr(self.runtime, "choice_provider", None),
            observe=self._observe_condition,
            emit=lambda event, **payload: self.runtime.trace.emit(event, activity_id=activity.activity_id, **payload),
            records=activity.records,
            result_judge=getattr(getattr(self.runtime, "body_loop", None), "decide_plan_result", None),
        )
        try:
            wait_attempt = 0
            while True:
                if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                    return
                # A waiting plan does not monopolize the physical Body.  It
                # reacquires the claim only for the next real call/choice.
                while True:
                    try:
                        self.runtime.body_control.claim(activity.activity_id, holder="agent_plan")
                        break
                    except RuntimeError as exc:
                        reason = str(exc)
                        if reason.startswith("body_busy:"):
                            activity.status = "waiting"
                            self.runtime.trace.emit(
                                "reflex_activity_waiting_current",
                                activity_id=activity.activity_id,
                                reason=reason,
                            )
                            await asyncio.sleep(0.1)
                            if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                                return
                            continue
                        if reason == "body_control_unknown":
                            native = await self._probe_native_stop()
                            if native:
                                self.runtime.body_control.confirm_native_stopped(True)
                                self.runtime.trace.emit(
                                    "reflex_activity_control_recovered",
                                    activity_id=activity.activity_id,
                                )
                                continue
                            if activity.revision != run_revision:
                                return
                            activity.status = "blocked"
                            activity.result = WorkRunResult(
                                "blocked", self._tree(activity), {},
                                {"status": "blocked", "reason": "control_unknown", "native_stopped": False},
                            )
                            self.runtime.trace.emit(
                                "reflex_activity_blocked",
                                activity_id=activity.activity_id,
                                reason="control_unknown",
                            )
                            self._notify_activity(activity)
                            return
                        if activity.revision != run_revision:
                            return
                        activity.status = "waiting"
                        activity.result = WorkRunResult("waiting", self._tree(activity), {}, {"reason": reason})
                        self.runtime.trace.emit(
                            "reflex_activity_waiting_current",
                            activity_id=activity.activity_id,
                            reason=reason,
                        )
                        return
                result = await executor.run()
                if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                    return
                activity.result = result
                activity.records = executor.records
                activity.status = result.status
                if result.status != "waiting":
                    break
                wait_attempt += 1
                self.runtime.trace.emit("reflex_activity_waiting", activity_id=activity.activity_id,
                                        tree=result.tree, facts=result.facts,
                                        next_check_s=min(2.0, 0.25 * (2 ** min(wait_attempt - 1, 3))))
                if self.runtime.body_control.owns(activity.activity_id):
                    self.runtime.body_control.settle(claim_id=activity.activity_id)
                # Keep the activity alive, but cap Decision traffic and allow
                # direct calls to use an idle Body while this plan waits.
                await asyncio.sleep(min(2.0, 0.25 * (2 ** min(wait_attempt - 1, 3))))
                if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                    return
            if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                return
            self.runtime.trace.emit("reflex_activity_finished", activity_id=activity.activity_id,
                                    status=result.status, tree=result.tree)
            # The fact goes out with the status it reports; the counters are
            # brought up to date long before the woken Agent can act on it.
            self._notify_activity(activity)
            await self._note_plan_outcome(activity, result.status, state_before)
        except asyncio.CancelledError:
            if activity.revision == run_revision and activity.status not in {"paused", "cancelled", "updating"}:
                activity.status = "cancelled"
            self.runtime.trace.emit("reflex_activity_cancelled", activity_id=activity.activity_id)
            raise
        except Exception as exc:
            if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                return
            activity.status = "blocked"
            self.runtime.trace.emit("reflex_activity_failed", activity_id=activity.activity_id,
                                    error_type=type(exc).__name__, message=str(exc)[:400])
            self._notify_activity(activity)
        finally:
            if self._active_activity_id == activity.activity_id:
                self._active_activity_id = None
            if (
                activity.status not in {"stopping", "cancelled", "paused", "updating"}
                and self.runtime.body_control.owns(activity.activity_id)
            ):
                self.runtime.body_control.settle(claim_id=activity.activity_id)

    async def _state(self) -> dict[str, Any]:
        try:
            getter = (getattr(self.runtime.body, "get_reflex_facts", None)
                      or self.runtime.body.get_state)
            state = await self.runtime._read_lane.run(getter)
            if isinstance(state, dict):
                return copy.deepcopy(state)
            if is_dataclass(state):
                return copy.deepcopy(asdict(state))
            values = getattr(state, "__dict__", None)
            return copy.deepcopy(values) if isinstance(values, dict) else {}
        except Exception:
            # A plan can run while the Agent has no turns. Its cache may be
            # the observation before that plan, so use it only if sampling
            # fails, never in preference to the present Body observation.
            return copy.deepcopy(self.runtime.last_known_body_state or {})

    async def _observe_condition(self, condition: dict[str, Any]) -> dict[str, Any]:
        """Read one generic object projection for non-body bindings.

        The work language does not contain scene-specific detectors. A named
        player/entity binding is resolved through the existing bounded entity
        perception, and the complete/age metadata remains visible to
        Decision. If the provider cannot expose the read, the binding stays
        unknown instead of being treated as absent.
        """
        bindings = condition.get("bindings") if isinstance(condition, dict) else None
        if not isinstance(bindings, dict):
            return {}
        handles = [str(value) for name, value in bindings.items()
                   if str(name) not in {"bot", "body"} and str(value) not in {"bot", "body"}]
        if not handles:
            return {}
        registry = getattr(self.runtime, "registry", None)
        run_tool = getattr(self.runtime, "run_tool", None)
        if registry is None or not callable(run_tool):
            return {}
        try:
            tool = registry.get("read_nearby_entities")
        except Exception:
            return {}
        try:
            result = await run_tool(
                tool,
                execute_tool,
                tool,
                {"radius": 64, "limit": 128},
                self.runtime.weld_context,
                timeout_s=getattr(tool.sidecar, "timeout_s", 20.0),
                tool_call_id=f"plan-observe-{self._active_activity_id or 'unknown'}",
                claim_holder="agent_plan",
                body_claim_id=self._active_activity_id,
                retain_claim=True,
            )
        except Exception as exc:
            self.runtime.trace.emit("reflex_condition_observation_failed",
                                    activity_id=self._active_activity_id,
                                    error_type=type(exc).__name__)
            return {}
        metrics = result.get("metrics") if isinstance(result, dict) else None
        data = metrics.get("data") if isinstance(metrics, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
            return {}
        sampled_at = data.get("sampled_at_ms") or metrics.get("sampled_at_ms")
        return {"nearby_entities": {
            "entities": copy.deepcopy(data["entities"]),
            "complete": bool(metrics.get("complete", False)),
            "radius": 64,
            "sampled_at_ms": sampled_at,
        }}

    async def _call(
        self, tool_name: str, arguments: dict[str, Any], *, call_id: str,
        timeout_s: float | None, supervision_context: dict[str, Any],
    ) -> dict[str, Any]:
        tool = self.runtime.registry.get(tool_name)
        raw = await self.runtime.run_tool(
            tool,
            _execute_plan_call,
            tool,
            arguments,
            self.runtime.weld_context,
            timeout_s=timeout_s or tool.sidecar.timeout_s,
            tool_call_id=f"plan-{call_id}",
            claim_holder="agent_plan",
            body_claim_id=self._active_activity_id,
            retain_claim=self._active_activity_id is not None,
            return_execution_receipt=True,
            supervision_context=supervision_context,
        )
        if not isinstance(raw, dict):
            return raw
        # Plan calls execute outside the Agent tool wrapper, whose settlement
        # normally records these observations. Update from the real result
        # before the executor takes its terminal Body snapshot or projects it.
        remember_body = getattr(self.runtime, "remember_tool_body_facts", None)
        if callable(remember_body):
            remember_body(raw)
        # WorkPlanExecutor must judge the same bounded projector that the
        # Agent sees for an ordinary tool call. Preserve supervision handoff
        # facts privately so the interpreter can record the Decision choice
        # without putting raw metrics back into the question.
        projector = getattr(self.runtime, "shown_plan_result", None)
        shown = projector(tool_name, raw, f"plan-{call_id}") if callable(projector) else dict(raw)
        metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
        handoff = metrics.get("direct_call_handoff")
        if isinstance(handoff, dict):
            shown["_supervision_handoff"] = copy.deepcopy(handoff)
        if isinstance(raw.get("_execution"), dict):
            shown["_execution"] = copy.deepcopy(raw["_execution"])
        return shown

    def _direct_call(self) -> dict[str, Any] | None:
        """The direct Body call still running after its turn stopped waiting."""
        call = getattr(self.runtime, "background_call", None)
        if call is None or call.task.done():
            return None
        return call.payload()

    def active_summary(self) -> dict[str, Any] | None:
        """One line of the live plan for the Agent's current-context snapshot."""
        with self._lock:
            activity = next((item for item in self._activities.values()
                             if item.status in {"accepted", "running", "waiting", "stopping", "updating"}), None)
            if activity is None:
                return None
            return {"activity_id": activity.activity_id, "status": activity.status,
                    "goal": str(activity.plan.get("goal", ""))[:200],
                    "elapsed_s": round(time.time() - activity.created_at, 1)}

    def read(self, activity_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            direct = self._direct_call()
            if direct is not None and activity_id in {None, direct["tool_call_id"]}:
                # A direct call is one call node without a plan around it.
                return ToolResult(True, "ok", True, metrics={
                    "activity_id": direct["tool_call_id"],
                    "status": "running",
                    "direct_call": direct,
                    "execution": self._execution(),
                    "body_control": self.runtime.body_control.snapshot().to_payload(),
                }).to_payload()
            activity = self._select(activity_id)
            if activity is None:
                return ToolResult(False, "no_reflex", True).to_payload()
            return ToolResult(True, "ok", True, metrics={
                "activity_id": activity.activity_id,
                "status": activity.status,
                "revision": activity.revision,
                "tree": self._shown_tree(activity),
                "facts": self._shown_facts(activity),
                "execution": self._execution(),
                "body_control": self.runtime.body_control.snapshot().to_payload(),
            }).to_payload()

    def _execution(self) -> dict[str, Any] | None:
        """What the Body operation in flight is doing right now, if any."""
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        return None if snapshots is None else snapshots.current()

    def control(self, params: dict[str, Any]) -> dict[str, Any]:
        operation = str(params.get("operation", ""))
        activity_id = params.get("activity_id")
        with self._lock:
            activity = self._select(str(activity_id) if activity_id else None)
            direct = self._direct_call()
            if direct is not None and operation in {"cancel", "take_control"} and (
                    not activity_id or activity_id == direct["tool_call_id"]):
                # Stopping a direct call uses the one stop entry; its own
                # final result then reports what the stop left behind.
                activity = None
                operation = "take_control"
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
                                      metrics={"settlement_pending": True, "task": id(future)}).to_payload()
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
                    activity.task = self._submit(self._run(activity))
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
                    return ToolResult(True, "updated", True, metrics={"activity_id": activity.activity_id, "tree": self._shown_tree(activity)}).to_payload()

                # Publish the new revision synchronously. Native stop and the
                # replacement run remain asynchronous, but an immediate read
                # must not expose the old terminal status and old plan.
                activity.status = "updating"
                activity.cancel_reason = "update"
                activity.revision += 1
                activity.plan = copy.deepcopy(spec)
                activity.result = None

                async def replace_plan() -> None:
                    revision = activity.revision
                    old_task = activity.task
                    if old_task is not None and not old_task.done():
                        old_task.cancel()
                    stop = getattr(self.runtime, "cancel_active_execution_with_facts", None)
                    if callable(stop):
                        facts = await stop("reflex_update", cancel_reflex=False)
                        if not facts.get("settled", False):
                            if activity.revision == revision:
                                activity.status = "blocked"
                            return
                    if old_task is not None and old_task is not asyncio.current_task():
                        await self._await_task(old_task)
                    if activity.revision != revision:
                        return
                    activity.cancel_reason = None
                    activity.status = "running"
                    activity.task = self._submit(self._run(activity))

                self._submit(replace_plan())
                return ToolResult(True, "updated", True, metrics={
                    "activity_id": activity.activity_id,
                    "status": "updating",
                    "revision": activity.revision,
                }).to_payload()
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
                try:
                    await self._await_task(task)
                except asyncio.CancelledError:
                    pass
            if facts.get("settled", False):
                activity.status = final_status
                previous = activity.result
                activity.result = WorkRunResult(
                    final_status,
                    self._tree(activity),
                    copy.deepcopy(previous.results) if previous is not None else {},
                    {
                        **(copy.deepcopy(previous.facts) if previous is not None else {}),
                        "status": final_status,
                    },
                )
            else:
                activity.status = "blocked"
                previous = activity.result
                activity.result = WorkRunResult(
                    "blocked",
                    self._tree(activity),
                    copy.deepcopy(previous.results) if previous is not None else {},
                    {
                        **(copy.deepcopy(previous.facts) if previous is not None else {}),
                        "status": "blocked",
                        "reason": "native_stop_unconfirmed",
                    },
                )
            self.runtime.trace.emit(
                "reflex_activity_stopped",
                activity_id=activity.activity_id,
                status=activity.status,
                reason=reason,
                settlement=facts,
            )
            self._notify_activity(activity)
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
        return ToolResult(True, "stop_requested", True, metrics={
            "activity_id": activity.activity_id,
            "status": "stopping",
            "settlement_pending": True,
            "task": id(future),
        }).to_payload()

    async def _probe_native_stop(self) -> bool:
        """Confirm the native Body is idle before retrying an unknown claim."""
        probe = getattr(self.runtime.body, "confirm_control_idle", None)
        if not callable(probe):
            return False
        try:
            lane = getattr(self.runtime, "_read_lane", None)
            if lane is not None and callable(getattr(lane, "run", None)):
                native = await lane.run(probe)
            else:
                native = await asyncio.to_thread(probe)
        except Exception as exc:
            self.runtime.trace.emit(
                "reflex_activity_control_probe_failed",
                activity_id=self._active_activity_id,
                error_type=type(exc).__name__,
            )
            return False
        return isinstance(native, dict) and native.get("native_stopped") is True

    def _select(self, activity_id: str | None) -> ReflexActivity | None:
        if activity_id:
            return self._activities.get(activity_id)
        return next(reversed(self._activities.values()), None) if self._activities else None

    @staticmethod
    def _shown_facts(activity: ReflexActivity) -> dict[str, Any]:
        """Run facts without the call results, which the tree already shows per node."""
        if activity.result is None:
            return {}
        return {key: copy.deepcopy(value) for key, value in activity.result.facts.items() if key != "results"}

    def _shown_tree(self, activity: ReflexActivity) -> dict[str, Any]:
        """The plan tree as the Agent reads it.

        The executor keeps every call's original result, because later nodes
        and conditions refer to it. The Agent reads each of them the way it
        reads a direct call: projected, with a handle to the stored original.
        """
        tree = copy.deepcopy(self._tree(activity))
        project = getattr(self.runtime, "shown_plan_result", None)
        if not callable(project):
            return tree

        def show(runtime: Any, tool: Any, ref: str) -> None:
            if not isinstance(runtime, dict):
                return
            result = runtime.get("result")
            if isinstance(result, dict) and isinstance(tool, str):
                key = f"{ref}:{runtime.get('updated_at')}"
                if key not in activity.shown:
                    activity.shown[key] = project(tool, result, f"plan-{ref}")
                runtime["result"] = copy.deepcopy(activity.shown[key])
            blocked = runtime.get("blocked")
            if isinstance(blocked, dict) and "result" in blocked and "result" in runtime:
                # The same result twice; the reason is what the block adds.
                blocked.pop("result")

        def walk(node: Any, path: str) -> None:
            if not isinstance(node, dict):
                return
            ref = node.get("id") if isinstance(node.get("id"), str) and node.get("id") else path
            runtime = node.get("_runtime")
            show(runtime, node.get("tool"), ref)
            kind = node.get("kind")
            if kind == "sequence":
                for index, child in enumerate(node.get("steps", [])):
                    walk(child, f"{path}.steps[{index}]")
            elif kind == "batch":
                body = node.get("body")
                walk(body, f"{path}.body")
                instances = runtime.get("instances") if isinstance(runtime, dict) else None
                if isinstance(instances, dict) and isinstance(body, dict):
                    for index, instance in instances.items():
                        show(instance, body.get("tool"), f"{ref}[{index}]")
            elif kind == "choice":
                for option in node.get("options", []):
                    if isinstance(option, dict):
                        walk(option.get("work"), f"{path}.options.{option.get('id')}")

        walk(tree.get("work"), "work")
        return tree

    def _tree(self, activity: ReflexActivity) -> dict[str, Any]:
        if activity.result is not None:
            return activity.result.tree
        try:
            executor = WorkPlanExecutor(activity.plan, call=self._call, state=self._state,
                                        decision=self.runtime.decision_client,
                                        choice_provider=getattr(self.runtime, "choice_provider", None),
                                        observe=self._observe_condition,
                                        records=activity.records)
            return executor.annotated_tree()
        except Exception:
            return copy.deepcopy(activity.plan)


# The tree with projected results is what read_reflex is asked for.
REFLEX_TREE_CHARS = 12_000


def reflex_observation_projector(reason: str, metrics: dict[str, object]) -> dict[str, Any]:
    tree = metrics.get("tree")
    if not isinstance(tree, dict):
        return {}
    shown, whole = answer_value(tree, budget=REFLEX_TREE_CHARS)
    return {"tree": shown, "tree_complete": whole}


def register_reflex_tools(registry: ToolRegistry, manager: ReflexManager) -> None:
    registry.register(RegisteredTool(
        "delegate_work",
        "Use this tool when a goal involves multiple dependent steps, a long-running action followed by later actions, a condition that may remain pending, or work that may need pause, resume, cancellation, or revision. The reflex layer walks the submitted tree one step at a time from bounded facts; you do not need to poll or wait after delegating, and terminal results are delivered asynchronously. The tree is a range of allowed steps and intents, not a perfect script.\n\nFor work that means ‘do this until enough’, express the range as repeat around a call or choice and write one natural-language ending condition. The reflex layer decides whether the current facts satisfy that condition, whether the same call can make progress again, or whether to return control. After handing over a plan, use your turn to prepare later reasoning instead of reading progress.\n\nSubmit one minebot-work/0.2 plan object with: language='minebot-work/0.2', goal (string), positive timeout_s, a required work.intent, and work (the root node). Every mutating direct body call also requires an intent field. Intent states the result to achieve, not the thought or tactic while starting it: good: “把 2 块生铁熔炼成 2 个铁锭”; bad: “我先把东西放进炉子”。 Child nodes may inherit their parent's intent; read-only calls do not need one. Missing intent is rejected.\n\nNode kinds:\n- call   {kind:'call', tool, arguments, optional id/when/timeout_s/repeat} — one tool call; Decision judges its result and may repeat it\n- sequence {kind:'sequence', intent, steps:[...]} — ordered range of steps; intent is required and must describe the composite action\n- wait   {kind:'wait', condition:{text,...}, timeout_s} — hold until a condition is satisfied or times out\n- batch  {kind:'batch', intent, items:[...], body:<node>} — run body once per item; intent required\n- choice {kind:'choice', intent, options:[{id,intent,work},...]} — Decision picks among the declared branches whenever the choice is entered; every option needs id, intent and work\n\nA call repeat is {again_when:<text or object>, until:<text or object>}; those conditions are interpreted by Decision after each result. The same-call option is always available even when repeat is omitted; repeat adds the Agent’s extra wording. interrupt_when is reserved for a later slice.\n\nsequence, batch, choice, and every choice option all require a non-empty intent field.\n\nExample multi-step plan (move then place):\n  {language:'minebot-work/0.2', goal:'go to [10,64,20] and place oak_log at [10,65,20]', timeout_s:60,\n   work:{kind:'sequence', intent:'travel then build',\n     steps:[\n       {kind:'call', tool:'move_to', arguments:{x:10,y:64,z:20}},\n       {kind:'call', tool:'place_block', arguments:{block:'oak_log',x:10,y:65,z:20}}\n     ]}}\n\nUse only registered tool names and keep arguments as the tool schema requires. Let the reflex layer judge enough, completion and off-intent from facts; do not encode scene detectors or a polling loop in the plan.",
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
        projector=reflex_observation_projector,
    ))
    registry.register(RegisteredTool(
        "read_reflex", "Read the annotated plan tree, real results, and current Body control.",
        {"type": "object", "properties": {"activity_id": {"type": "string"}}, "additionalProperties": False},
        lambda params: manager.read(params.get("activity_id")),
        ToolSidecar("reflex_read", mutating=False, source="agent.reflex", tool_type="plan", permission="observe_reflex"),
        projector=reflex_observation_projector,
    ))


__all__ = ["ReflexActivity", "ReflexManager", "register_reflex_tools"]
