"""The per-Body reflex loop.

The first reflex slice grew around :class:`CallSupervisor`, whose lifetime was
one physical call.  ``BodyLoop`` is the owner for the lifetime of the Body:
the existing call supervision remains the work-state implementation while the
loop also has an explicit lifecycle and an idle frame source.  Keeping the
old class as a base preserves the narrow SDK seams used by existing callers;
there is still one instance and one Decision client for a Body.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from uuid import uuid4
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from minebot.app import reflex_question as rq
from minebot.app import direct_call_supervision as supervision_module
from minebot.app.direct_call_supervision import (
    CallSupervisor,
    FACT_FRAME_INTERVAL_S,
)


class BodyLoop(CallSupervisor):
    """Own supervision for work calls and the Body's idle lifecycle."""

    def __init__(self, runtime: Any, **kwargs: Any) -> None:
        super().__init__(runtime, **kwargs)
        self.loop_state = "idle"
        self._lifecycle_task: asyncio.Task[Any] | None = None
        self._lifecycle_started = False
        self._idle_sequence = 0
        self.recent_experiences: deque[str] = deque(maxlen=5)

    async def run(self, tool: Any, callback: Any, args: tuple[Any, ...], **kwargs: Any) -> Any:
        """Keep the lifecycle state aligned with the one physical call path."""
        context = kwargs.get("supervision_context") or {}
        context = context if isinstance(context, Mapping) else {}
        previous = self.loop_state
        if context.get("is_reflex_action"):
            choice = context.get("reflex_action_choice")
            target = "threat" if choice == "move_away" else "survival"
        else:
            target = "work"
        self._set_state(target, "agent_call" if target == "work" else "decision")
        try:
            return await super().run(tool, callback, args, **kwargs)
        finally:
            self._set_state(previous, "call_ended")
            self._ensure_idle_task()

    def _start_monitor(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
        wake: asyncio.Event, worker: asyncio.Task[Any],
    ) -> asyncio.Task[Any]:
        """Use the Body lifecycle slot for a normal Agent call.

        The idle reader is cancelled before a work call takes the slot.  A
        nested protective call is already running inside the lifecycle task;
        it gets a child monitor while the parent task remains the owner and
        resumes the lifecycle after the protective call settles.
        """
        current_task = asyncio.current_task()
        lifecycle = self._lifecycle_task
        if lifecycle is not None and lifecycle is not current_task and not lifecycle.done():
            lifecycle.cancel()
        task = asyncio.create_task(self._monitor(record, tool, params, wake, worker))
        if lifecycle is not current_task:
            self._lifecycle_task = task
        return task

    async def _monitor(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
        wake: asyncio.Event, worker: asyncio.Task[Any],
    ) -> None:
        """Consume work facts and execute the one in-flight Decision answer.

        ``CallSupervisor`` supplies the call registration and settlement
        helpers; this Body-owned method is the only frame consumer.  A newer
        fact replaces a pending frame while a Decision request is in flight.
        """
        last_revision = -1
        decision_task: asyncio.Task[str] | None = None
        pending: tuple[dict[str, Any], float, str] | None = None
        decision_revision = -1
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        while not record["cancel_requested"]:
            if worker.done() and decision_task is None:
                return
            wake_wait = asyncio.create_task(wake.wait())
            clock_wait = asyncio.create_task(asyncio.sleep(supervision_module.FACT_FRAME_INTERVAL_S))
            wait_set: set[asyncio.Future[Any]] = {wake_wait, clock_wait}
            if decision_task is not None:
                wait_set.add(decision_task)
            if not worker.done():
                wait_set.add(worker)
            done, _pending_tasks = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            finished_decision = decision_task if decision_task is not None and decision_task in done else None
            if clock_wait in done and not worker.done() and snapshots is not None:
                snapshots.tick(record["call_id"])
            if wake_wait in done or clock_wait in done:
                wake.clear()
                execution = self._frame(self._snapshot(record["call_id"]))
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
                            record["initial_reader_done"] = True
                            await self._bounded_read(record, tool, params)
                            execution = self._frame(self._snapshot(record["call_id"]) or execution)
                        frame_options = self._options_for(
                            tool, record,
                            supplied=self._tree_options(record["supervision_context"]),
                            execution=execution,
                        )
                        if decision_task is not None and not decision_task.done():
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
                    "intent": supervision_module._DEFAULT_ACTION_TEXT.get(choice, choice),
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
                                         "intent": supervision_module._DEFAULT_ACTION_TEXT.get(action[0], action[0])},
                )
            except Exception as exc:
                default_result = {"success": False, "reason": type(exc).__name__, "canRetry": False}
        return "call_agent"


    def _set_state(self, state: str, cause: str) -> None:
        if state == self.loop_state:
            return
        previous = self.loop_state
        self.loop_state = state
        self.runtime.trace.emit(
            "reflex_state_changed", **{"from": previous, "to": state, "cause": cause}
        )

    def record_experience(self, text: str) -> None:
        """Keep the short reflex handoff history used by live Agent context."""
        value = str(text).strip()
        if value:
            self.recent_experiences.append(value[:500])

    async def decide_plan_result(
        self, *, call_id: str, tool: Any, params: Mapping[str, Any],
        plan_context: Mapping[str, Any], decision_context: Mapping[str, Any],
        next_call: str = "", repeat_note: str = "",
    ) -> str:
        """Judge a plan call's terminal fact through this Body's loop.

        WorkPlanExecutor supplies facts and the tree range; it no longer owns a
        second Decision client/question protocol in production.  Unknown stays
        unknown and lets the lifecycle loop ask again while the Body is idle.
        """
        result = decision_context.get("tool_result")
        result = result if isinstance(result, Mapping) else {}
        facts = decision_context.get("body_state")
        facts = facts if isinstance(facts, Mapping) else {}
        execution = decision_context.get("execution")
        execution = dict(execution) if isinstance(execution, Mapping) else {}
        execution.setdefault("tool", getattr(tool, "name", "unknown"))
        execution["state"] = "succeeded" if result.get("success") is True else "failed"
        execution["facts"] = dict(facts)
        options = rq.frame_options(
            tool=str(getattr(tool, "name", "unknown")), params=params,
            intent=str(plan_context.get("intent") or ""), facts=facts,
            running=False, result=result, next_call=next_call,
            repeat_note=repeat_note,
        )
        options = {key: value for key, value in options.items()
                   if key not in rq.PROTECTIVE and key != "inspect"}
        return await self._judge_frame(
            call_id, tool, params, execution, 0.0,
            purpose="plan_result_frame", supervision_context=plan_context,
            options=options, decision_context=decision_context,
        )

    async def start(self) -> None:
        """Start the one lifecycle task when the SDK session enters the world."""
        if self._lifecycle_started:
            return
        self._lifecycle_started = True
        self.runtime.trace.emit("reflex_loop_started", loop_state=self.loop_state)
        self._lifecycle_task = asyncio.create_task(self._idle_frames())

    def _ensure_idle_task(self) -> None:
        if not self._lifecycle_started or self.current is not None:
            return
        task = self._lifecycle_task
        if task is None or task.done():
            self._lifecycle_task = asyncio.create_task(self._idle_frames())

    async def stop(self) -> None:
        """Stop the lifecycle task before the runtime closes its read lane."""
        task = self._lifecycle_task
        self._lifecycle_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._lifecycle_started:
            self.runtime.trace.emit("reflex_loop_stopped", loop_state=self.loop_state)
        self._lifecycle_started = False

    def _idle_facts(self) -> dict[str, Any]:
        cached = getattr(self.runtime, "last_known_body_state", None)
        if isinstance(cached, Mapping):
            return dict(cached)
        return {}

    async def _read_idle_facts(self) -> dict[str, Any]:
        """Read through the existing read lane; never use the execution lane."""
        body = getattr(self.runtime, "body", None)
        getter = getattr(body, "get_state", None)
        read_lane = getattr(self.runtime, "_read_lane", None)
        if callable(getter) and read_lane is not None:
            try:
                value = await read_lane.run(getter)
                if isinstance(value, Mapping):
                    return dict(value)
                if is_dataclass(value):
                    return asdict(value)
                raw = getattr(value, "__dict__", None)
                if isinstance(raw, dict):
                    return dict(raw)
            except Exception as exc:
                self.runtime.trace.emit(
                    "reflex_idle_read_unknown", error_type=type(exc).__name__
                )
        return self._idle_facts()

    @staticmethod
    def _idle_options(facts: Mapping[str, Any]) -> dict[str, str]:
        """The Step-2 idle range: reversible waiting plus applicable protection."""
        options = {"stay": "Stay where the Body is and wait for the next step."}
        air = facts.get("oxygen")
        if isinstance(air, (int, float)) and air < rq.AIR_FULL:
            options["surface"] = "Swim up to air."
        food_item, food = facts.get("food_item"), facts.get("food")
        if isinstance(food_item, str) and food_item and isinstance(food, (int, float)) and food < 20:
            options["eat"] = f"Eat {food_item} from the inventory."
        near = rq.hostiles(facts)
        if near and near[0].get("dist", float("inf")) <= rq.HOSTILE_REACH:
            options["move_away"] = "Move away from the nearby hostile entity."
        safe = facts.get("safe_standing_position", facts.get("last_safe_position"))
        if rq.just_hurt(facts) and rq._position(safe):
            options["retreat_safe"] = "Go back to the last place where the Body stood safely."
        return options

    def _idle_question(self, facts: Mapping[str, Any], options: Mapping[str, str]) -> dict[str, Any]:
        body = rq._body_sentence(facts)
        state: dict[str, Any] = {"body": body}
        threats = rq._threat_sentence(facts)
        if threats:
            state["threats"] = threats
        inventory = facts.get("inventory", facts.get("inventory_counts"))
        if isinstance(inventory, Mapping):
            state["inventory"] = ", ".join(f"{k}={v}" for k, v in inventory.items()) or "empty"
        return {"state": state, "questions": rq.frame_questions(options)}

    async def _run_idle_protection(self, choice: str, facts: Mapping[str, Any]) -> None:
        """Execute one reversible idle protection action through run_tool."""
        action = self._default_action(choice, {"facts": dict(facts)})
        if action is None:
            return
        registry = getattr(self.runtime, "registry", None)
        default_tool = registry.get(action[0]) if registry is not None else None
        if default_tool is None:
            self.runtime.trace.emit("call_supervision_default_unknown", tool_call_id="idle-loop", choice=choice)
            return
        from minebot.brain.registry import execute_tool

        call_id = f"reflex-idle-{self._idle_sequence}"
        self.runtime.trace.emit(
            "call_supervision_default_requested", tool_call_id="idle-loop",
            choice=choice, tool=action[0], arguments=action[1], loop_state=self.loop_state,
        )
        try:
            result = await self.runtime.run_tool(
                default_tool, execute_tool, default_tool, action[1],
                getattr(self.runtime, "weld_context", None),
                tool_call_id=call_id, claim_holder="life",
                supervision_context={
                    "is_reflex_action": True,
                    "reflex_action_choice": choice,
                    "intent": {"surface": "Swim up to air.", "eat": "Eat food from the inventory.",
                               "move_away": "Move away from the nearby hostile mob.",
                               "retreat_safe": "Go back to the last safe standing place."}.get(choice, choice),
                },
            )
            compact = CallSupervisor._compact_result(result)
            self.record_experience(
                f"The Body took the protective action {choice} while idle; "
                f"the action result was {compact}."
            )
            self.runtime.trace.emit(
                "call_supervision_default_finished", tool_call_id="idle-loop",
                choice=choice, tool=action[0], result=compact,
            )
        except Exception as exc:
            self.runtime.trace.emit(
                "call_supervision_default_failed", tool_call_id="idle-loop",
                choice=choice, error_type=type(exc).__name__,
            )

    @staticmethod
    def _default_action(choice: str, execution: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        return CallSupervisor._default_action(choice, execution)

    async def _idle_frames(self) -> None:
        """Produce idle facts at the same cadence as running frames.

        Unknown answers remain inert.  A selected protective action is the
        only write this step executes while idle, and it uses the existing
        ``run_tool`` admission and settlement path.
        """
        try:
            while True:
                await asyncio.sleep(FACT_FRAME_INTERVAL_S)
                if self.current is not None:
                    continue
                snapshots = getattr(self.runtime, "execution_snapshots", None)
                if snapshots is not None and snapshots.current() is not None:
                    continue
                facts = await self._read_idle_facts()
                options = self._idle_options(facts)
                request = self._idle_question(facts, options)
                self._idle_sequence += 1
                started = time.monotonic()
                self.runtime.trace.emit(
                    "call_supervision_request", tool_call_id="idle-loop",
                    tool="__idle__", purpose="idle_frame", loop_state="idle",
                    fact_revision=self._idle_sequence, request=request,
                    request_chars=len(str(request)),
                )
                try:
                    # Idle is a live state, so still ask even when the only
                    # reversible normal option is ``stay``.  This keeps the
                    # same one-question-per-frame contract as a running call.
                    answer = await self._client.ask(request)
                    choice = rq.frame_choice(answer.raw, request["questions"])
                except Exception:
                    choice = "unknown"
                self.runtime.trace.emit(
                    "call_supervision_result", tool_call_id="idle-loop",
                    tool="__idle__", purpose="idle_frame", choice=choice,
                    unknown=choice == "unknown",
                    decision_ms=round((time.monotonic() - started) * 1000.0, 3),
                )
                if choice in rq.PROTECTIVE or choice == "eat":
                    await self._run_idle_protection(choice, facts)
        except asyncio.CancelledError:
            raise


__all__ = ["BodyLoop"]
