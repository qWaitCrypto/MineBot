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
import copy
import time
from collections import deque
from uuid import uuid4
from dataclasses import asdict, is_dataclass, replace
from typing import Any, Mapping
from minebot.contract import Action, Event, ToolResult

from minebot.app import reflex_question as rq
from minebot.app import direct_call_supervision as supervision_module
from minebot.app.direct_call_supervision import (
    CallSupervisor,
    FACT_FRAME_INTERVAL_S,
)
from minebot.app.life_profile import load_profile, life_intent
from minebot.app.execution_snapshot import TERMINAL_STATES


class BodyLoop(CallSupervisor):
    """Own supervision for work calls and the Body's idle lifecycle."""

    def __init__(self, runtime: Any, **kwargs: Any) -> None:
        super().__init__(runtime, **kwargs)
        self.loop_state = "idle"
        self._lifecycle_task: asyncio.Task[Any] | None = None
        self._lifecycle_started = False
        self._active_monitor: tuple[dict[str, Any], Any, Mapping[str, Any], asyncio.Event, asyncio.Task[Any]] | None = None
        self._result_pending: dict[str, dict[str, Any]] = {}
        self._terminal_questions: deque[tuple[asyncio.Future[str], tuple[Any, ...], dict[str, Any]]] = deque()
        self._loop_wakeup = asyncio.Event()
        self._idle_sequence = 0
        self._suspended: dict[str, Any] | None = None
        self._protection_task: asyncio.Task[Any] | None = None
        self._protective_redirect: tuple[str, tuple[str, dict[str, Any]]] | None = None
        self._protective_verdict: str | None = None
        self.recent_experiences: deque[str] = deque(maxlen=5)
        self.safe_standing_position: list[float] | None = None
        self._lifecycle_events: deque[Event] = deque()
        self._death_facts: dict[str, Any] | None = None
        self._death_count = 0
        self._native_lifecycle = False
        self._body_lifecycle_listener = None
        self._reaction_observations: dict[str, dict[str, Any]] = {}
        self._reaction_selection: dict[str, Any] | None = None
        self._reaction_actions: dict[str, dict[str, Any]] = {}
        self._reaction_event_loop: asyncio.AbstractEventLoop | None = None
        self._ambient_facts: dict[str, Any] = {}
        self._life_profile = load_profile()
        self._reflex_claim_holder = "threat"
        self._reflex_action_intent: str | None = None

    def _queue_lifecycle_event(self, event: Event) -> None:
        self._lifecycle_events.append(event)
        self._loop_wakeup.set()
        if self._active_monitor is not None:
            self._active_monitor[3].set()

    async def _process_lifecycle_events(self, facts: Mapping[str, Any] | None = None) -> bool:
        """Deterministic edges, consumed only by the one Body lifecycle task."""
        if (not self._lifecycle_events and rq.dead(facts or {})
                and self.loop_state != "dead" and not (self._native_lifecycle and self._death_facts)):
            self._lifecycle_events.append(Event(seq=0, tick=0, bot="", name="death", data=dict(facts or {})))
        if self._lifecycle_task is not None and asyncio.current_task() is not self._lifecycle_task:
            self._loop_wakeup.set()
            return self.loop_state == "dead" or any(e.name == "death" for e in self._lifecycle_events)
        while self._lifecycle_events:
            event = self._lifecycle_events.popleft()
            if event.name == "death":
                if self.loop_state == "dead":
                    continue
                self._set_state("dead", "death")
                self._death_count += 1
                self._reaction_observations.clear()
                self._reaction_actions.clear()
                self._reaction_selection = None
                self._ambient_facts.clear()
                death = dict(event.data)
                death["vital_samples"] = copy.deepcopy(self._vitals._samples)
                self._death_facts = death
                self.safe_standing_position = None
                self._result_pending.clear()
                self._suspended = None
                protection = self._protection_task
                self._protection_task = None
                if protection is not None and protection is not asyncio.current_task() and not protection.done():
                    protection.cancel()
                if self.current is not None:
                    self.current["cancel_requested"] = True
                    self.current["result_abandoned"] = True
                settled = await self.runtime.cancel_active_execution_with_facts("death")
                if protection is not None and protection is not asyncio.current_task():
                    await asyncio.gather(protection, return_exceptions=True)
                self.runtime.trace.emit("reflex_death", facts=death, settlement=settled)
                event = Event(seq=event.seq, tick=event.tick, bot=event.bot, name=event.name, data=death)
                notify = getattr(self.runtime, "notify_body_lifecycle_event", None)
                if callable(notify):
                    notify(event)
            elif event.name == "respawned":
                self._vitals = rq.VitalWindow()
                self._ambient_facts.clear()
                self.safe_standing_position = None
                self._result_pending.clear()
                self._set_state("idle", "respawned")
                self.runtime.trace.emit("reflex_respawned", facts=dict(event.data))
                notify = getattr(self.runtime, "notify_body_lifecycle_event", None)
                if callable(notify):
                    notify(event)
        return self.loop_state == "dead"

    def _frame(self, execution: Mapping[str, Any] | None) -> dict[str, Any]:
        if not execution:
            return {}
        # Native lite progress omits inventory and perception. That is not an
        # empty inventory or an observation that all entities have disappeared.
        # Keep only Body observations across calls, never the previous call's
        # phase, navigation, deltas or progress. Explicit observations replace
        # every alias of that observation, including explicit empty containers.
        ambient_before = copy.deepcopy(self._ambient_facts)
        observed = dict(execution.get("facts") or {})
        groups = (
            ("inventory", "inventory_counts"),
            ("nearby_hostiles", "hostiles", "nearby_entities", "entities", "entities_near"),
        )
        for group in groups:
            if any(key in observed for key in group):
                for key in group:
                    self._ambient_facts.pop(key, None)
                self._ambient_facts.update({key: copy.deepcopy(observed[key])
                                            for key in group if key in observed})
        for key in ("selected_item", "selected_item_known", "inventory_complete",
                    "inventory_includes_selected", "creative", "dimension", "time"):
            if key in observed:
                self._ambient_facts[key] = copy.deepcopy(observed[key])
        combined = {**copy.deepcopy(self._ambient_facts), **observed}
        if self._life_profile:
            combined["life_profile"] = copy.deepcopy(self._life_profile)
            combined["life_intent"] = life_intent(self._life_profile)
        combined["agent_thinking"] = bool(getattr(self.runtime, "agent_thinking", False))
        previous = copy.deepcopy(self.safe_standing_position)
        frame = super()._frame({**execution, "facts": combined})
        # Store both raw sources so replay can reproduce exactly what online
        # compilation saw, rather than presenting carried observations as raw
        # data supplied by the current tool.
        frame["_frame_input"]["execution"] = copy.deepcopy(
            {key: value for key, value in execution.items() if key != "_frame_input"})
        frame["_frame_input"]["ambient_facts"] = ambient_before
        facts = frame["facts"]
        health, before = facts.get("health"), facts.get("health_before")
        position = facts.get("position", facts.get("pos"))
        if (facts.get("on_ground") is True and facts.get("head_in_liquid") is False
                and isinstance(health, (int, float)) and health > 0
                and isinstance(before, (int, float)) and health >= before
                and isinstance(position, (list, tuple)) and len(position) == 3):
            self.safe_standing_position = list(position)
        if self.safe_standing_position is not None:
            facts["safe_standing_position"] = copy.deepcopy(self.safe_standing_position)
        frame["_frame_input"]["safe_standing_position"] = previous
        self._observe_reaction_frame(facts)
        return frame

    def _observe_reaction_frame(self, facts: Mapping[str, Any]) -> None:
        """Telemetry only: first frame in which an existing option is eligible."""
        near = rq.hostiles(facts)
        ids = set()
        hurt = rq.just_hurt(facts)
        for entity in near:
            identity = entity.get("id")
            if not isinstance(identity, str) or not identity:
                continue
            if entity["dist"] <= rq.HOSTILE_REACH or hurt:
                ids.add(identity)
                self._reaction_observations.setdefault(identity, {
                    "target": identity, "event_monotonic_s": time.monotonic(),
                    "event_sampled_at_ms": facts.get("sampled_at_ms"),
                    "event_health": facts.get("health"), "event_distance": entity["dist"],
                })
        self._reaction_observations = {key: value for key, value in self._reaction_observations.items() if key in ids}

    def _reaction_question(self, facts: Mapping[str, Any], stamp: float) -> dict[str, Any] | None:
        near = rq.hostiles(facts)
        observed = self._reaction_observations.get(near[0].get("id")) if near else None
        return None if observed is None else {**observed, "request_monotonic_s": stamp}

    def _reaction_answer(self, chain: dict[str, Any] | None, choice: str) -> None:
        if chain is not None and choice in {"engage", "ranged", "move_away", "retreat_safe"}:
            self._reaction_selection = {**chain, "choice": choice, "answer_monotonic_s": time.monotonic()}

    def observe_native_action_progress(self, operation_id: str, payload: Mapping[str, Any]) -> None:
        """Called by the existing observation lane; no new sampler or loop."""
        event_loop = self._reaction_event_loop
        if event_loop is not None and not event_loop.is_closed() and payload.get("action_id"):
            event_loop.call_soon_threadsafe(self._reaction_progress, operation_id, time.monotonic(),
                                           payload.get("action_id"), payload.get("sampled_at_ms"))

    def _reaction_progress(self, operation_id: str, stamp: float, native_id: Any, sampled_at: Any) -> None:
        chain = self._reaction_actions.pop(operation_id, None)
        if chain is not None:
            self.runtime.trace.emit("reflex_reaction_chain", **chain,
                tool_call_id=operation_id, action_progress_monotonic_s=stamp,
                native_action_id=native_id, action_sampled_at_ms=sampled_at,
                event_to_request_s=chain["request_monotonic_s"]-chain["event_monotonic_s"],
                request_to_answer_s=chain["answer_monotonic_s"]-chain["request_monotonic_s"],
                answer_to_action_s=stamp-chain["answer_monotonic_s"],
                total_s=stamp-chain["event_monotonic_s"], first_native_progress=True)

    async def judge(self, call_id, tool, params, execution, idle_s, **kwargs):
        facts = execution.get("facts") or {}
        chain = self._reaction_question(facts, time.monotonic())
        choice = await super().judge(call_id, tool, params, execution, idle_s, **kwargs)
        self._reaction_answer(chain, choice)
        return choice

    async def run(self, tool: Any, callback: Any, args: tuple[Any, ...], **kwargs: Any) -> Any:
        """Keep the lifecycle state aligned with the one physical call path."""
        context = kwargs.get("supervision_context") or {}
        context = context if isinstance(context, Mapping) else {}
        previous = self.loop_state
        death_count = self._death_count
        await self.start()
        if await self._process_lifecycle_events():
            return {"success": False, "reason": "death", "canRetry": False}
        call_id = str(kwargs.get("call_id") or "")
        self._result_pending[call_id] = {"call_id": call_id}
        if context.get("is_life_action"):
            target = "idle"
        elif context.get("is_reflex_action"):
            choice = context.get("reflex_action_choice")
            target = "threat" if choice in {"move_away", "engage", "ranged"} else "survival"
        else:
            target = "work"
        self._set_state(target, "agent_call" if target == "work" else "decision")
        try:
            return await super().run(tool, callback, args, **kwargs)
        finally:
            # A protective terminal still awaits Decision's return/handoff
            # choice. Keep its survival/threat state through that result.
            if not context.get("is_reflex_action") and death_count == self._death_count:
                self._set_state(previous, "call_ended")
            handoff = getattr(self.runtime, "direct_call_handoffs", {}).get(call_id, {})
            decided = handoff.get("facts", {}).get("decision", {}).get("choice")
            if not context.get("is_plan") or decided in {"step_done", "next_step", "call_agent", "repeat", "interrupt"}:
                # WorkPlanExecutor consumes a running-frame verdict directly,
                # without asking another result question. There is therefore
                # no later decide_plan_result owner to release this slot.
                self._release_result_pending(call_id)
            self._ensure_idle_task()

    def _start_monitor(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
        wake: asyncio.Event, worker: asyncio.Task[Any],
    ) -> asyncio.Task[Any] | None:
        """All calls, including protective calls, use the lifecycle consumer."""
        if self._active_monitor is not None:
            raise RuntimeError("BodyLoop already has an active monitor")
        record["monitor_registered"] = True
        record["monitor_done"] = asyncio.Event()
        self._result_pending.pop(str(record.get("call_id") or ""), None)
        self._active_monitor = (record, tool, params, wake, worker)
        self._loop_wakeup.set()
        return None

    async def _wait_monitor(self, record: Mapping[str, Any]) -> None:
        done = record.get("monitor_done")
        if isinstance(done, asyncio.Event):
            await done.wait()
        transition = record.get("protection_task")
        if isinstance(transition, asyncio.Task):
            await transition

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
        decision_execution: dict[str, Any] | None = None
        pending: tuple[dict[str, Any], float, str] | None = None
        decision_revision = -1
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        last_body_read = float("-inf")
        while not record["cancel_requested"]:
            if await self._process_lifecycle_events():
                return
            if worker.done():
                if decision_task is not None:
                    decision_task.cancel()
                    await asyncio.gather(decision_task, return_exceptions=True)
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
                # Lite action progress carries motion and vitals, but not
                # perception. Refresh the same Body read used while idle;
                # carrying the last call's entities cannot observe a new
                # attacker (or one that has gone away). This stays in the
                # one lifecycle consumer and the existing read lane.
                if time.monotonic() - last_body_read >= supervision_module.FACT_FRAME_INTERVAL_S:
                    await self._refresh_running_facts(record["call_id"])
                    last_body_read = time.monotonic()
                execution = self._frame(self._snapshot(record["call_id"]))
                if await self._process_lifecycle_events(execution.get("facts")) or record["cancel_requested"]:
                    if decision_task is not None:
                        decision_task.cancel()
                        await asyncio.gather(decision_task, return_exceptions=True)
                    for waiter in (wake_wait, clock_wait):
                        waiter.cancel()
                    await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)
                    return
                if execution:
                    revision = int(execution.get("fact_revision") or 0)
                    phase_changed = execution.get("phase") != record.get("last_phase")
                    record["last_phase"] = execution.get("phase")
                    if revision > last_revision and (phase_changed or revision != last_revision):
                        last_revision = revision
                        idle_s = float(execution.get("since_progress_s") or 0.0)
                        if (not record["initial_reader_done"]
                                and callable(getattr(tool, "supervision_reader", None))):
                            record["initial_reader_done"] = True
                            await self._bounded_read(record, tool, params)
                            execution = self._frame(self._snapshot(record["call_id"]) or execution)
                        frame = (execution, idle_s, "running_frame")
                        frame_options = self._options_for(
                            tool, record,
                            supplied=self._tree_options(record["supervision_context"]),
                            execution=execution,
                        )
                        if decision_task is not None and not decision_task.done():
                            pending = frame
                        elif decision_task is None and execution.get("state") not in {"succeeded", "failed", "cancelled", "unknown"}:
                            decision_revision = revision
                            decision_execution = execution
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
                latest = self._snapshot(record["call_id"])
                if worker.done() or latest.get("state") in TERMINAL_STATES:
                    # This answer was about running work, which has already
                    # ended. Its one terminal question owns what follows; a
                    # late running answer cannot stop or redirect that result.
                    self.runtime.trace.emit(
                        "call_supervision_answer_discarded", tool_call_id=record["call_id"],
                        choice=choice, reason="call_ended", fact_revision=decision_revision,
                    )
                    for waiter in (wake_wait, clock_wait):
                        if not waiter.done():
                            waiter.cancel()
                    await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)
                    return
                # The option was compiled from this complete frame. Native
                # lite snapshots omit carried inventory/perception and the
                # life profile; rebinding against one silently loses actions
                # or sends the chosen action to a different target.
                execution = decision_execution or {}
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
                elif choice in {"step_done", "next_step", "call_agent", "interrupt"}:
                    record["decision"] = choice
                    plan = self._plan_context(
                        supervision_context=record["supervision_context"],
                        tool=tool, params=params,
                    )
                    normalized_choice = "step_done" if choice in {"step_done", "next_step"} else choice
                    record["decision"] = normalized_choice
                    reason = ("decision_step_done" if normalized_choice == "step_done" else
                              "decision_interrupt" if choice == "interrupt" else "decision_call_agent")
                    await self._handoff(
                        record, execution,
                        {"reason": reason, "confirmed": normalized_choice == "step_done"},
                        decision=choice, plan=plan,
                    )
                    return
                elif (choice in rq.PROTECTIVE or choice in {"go_to_surface", "eat"}
                      or (record["supervision_context"].get("is_life_action")
                          and choice in {"go_home", "approach_player", "follow_player", "look_player", "seek_player", "stay"})):
                    if choice == "stay":
                        await self._handoff(record, execution, {"reason": "decision_stay"}, decision=choice)
                        return
                    action = self._default_action(choice, execution)
                    if action is not None:
                        if record["supervision_context"].get("is_reflex_action"):
                            self._protective_redirect = (choice, action)
                            await self._handoff(record, execution,
                                {"reason": "decision_default_tool", "confirmed": False},
                                decision=choice)
                        else:
                            self._suspended = record
                            # The execution transaction waits outside this
                            # consumer. Return to the main loop so the next
                            # protective call registers as current work.
                            self._protection_task = asyncio.create_task(
                                self._protect_work(record, execution, choice, action))
                            record["protection_task"] = self._protection_task
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
                        decision_execution = frame[0]
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
        if self.loop_state == "dead" or self.current is not record or record["cancel_requested"]:
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
        is_protection = bool(record["supervision_context"].get("is_reflex_action"))
        if is_protection:
            self._protective_verdict = str(decision or "call_agent")
        started = time.monotonic()
        settled = await self.runtime.cancel_active_execution_with_facts(
            "direct_call:" + safe["reason"], cancel_reflex=False,
            # A plan handoff stops the current node but does not invalidate
            # the containing activity. Direct calls have no owner to preserve
            # and keep the existing generation invalidation safety boundary.
            invalidate_generation=not (is_plan or is_protection),
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
        if not is_plan and not is_protection:
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
        if await self._process_lifecycle_events(execution.get("facts")):
            return "unknown"
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
            choice = await asyncio.wait_for(self._judge_terminal_frame(
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
        candidates = rq.equipment_candidates(body, payload)
        if choice in candidates:
            choice = await self._run_result_equipment(record, tool, params, candidates[choice])
            if choice in {"repeat", "unknown"}:
                return choice
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
                extra={"tool_result": self._compact_result(payload),
                       **({"default_action": record["equipment_result"]}
                          if "equipment_result" in record else {})},
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

    async def _run_result_equipment(self, record, original_tool, original_params, item, *,
                                    claim_holder="agent_call", reflex_action_choice=None):
        """Result equipment uses the same judged single-node tree."""
        from minebot.app.work_runtime import WorkPlanExecutor
        from minebot.brain.registry import execute_tool
        budget = self._remaining(record)
        if budget is None or budget <= 0:
            return "unknown"
        await self.runtime.cancel_active_execution_with_facts(
            "direct_call:decision_equipment", cancel_reflex=False, invalidate_generation=False)
        node_id = f"equipment_{uuid4().hex}"
        spec = {"language": "minebot-work/0.2", "goal": str(record["supervision_context"].get("intent") or ""),
                "timeout_s": budget, "work": {"kind": "call", "id": node_id,
                    "tool": "equip_item", "intent": rq.equipment_intent(item),
                    "arguments": {"item": item, "target": "mainhand"}}}
        executor = None
        async def call(name, arguments, *, call_id, timeout_s):
            target = self.runtime.registry.get(name)
            transport_id = f"plan-{call_id}"
            context = executor.call_context(call_id)
            if claim_holder == "threat":
                context = {**context, "is_reflex_action": True, "reflex_action_choice": reflex_action_choice}
            raw = await self.runtime.run_tool(target, execute_tool, target, arguments,
                getattr(self.runtime, "weld_context", None), tool_call_id=transport_id,
                claim_holder=claim_holder, timeout_s=self._remaining(record),
                return_execution_receipt=True, supervision_context=context)
            projector = getattr(self.runtime, "shown_plan_result", None)
            shown = projector(name, raw, transport_id) if callable(projector) else dict(raw)
            if isinstance(raw.get("_execution"), dict):
                shown["_execution"] = raw["_execution"]
            handoff = (raw.get("metrics") or {}).get("direct_call_handoff")
            if isinstance(handoff, dict):
                shown["_supervision_handoff"] = copy.deepcopy(handoff)
            return shown
        executor = WorkPlanExecutor(spec, call=call, state=self._read_idle_facts,
                                    result_judge=self.decide_plan_result, emit=self.runtime.trace.emit)
        executor.plan["work"]["next_call"] = rq.call_text(getattr(original_tool, "name", "unknown"), original_params)
        while self._remaining(record) > 0:
            result = await asyncio.wait_for(executor.run(), timeout=self._remaining(record))
            record["equipment_result"] = {"tool": "equip_item", "result": result.results}
            if result.status == "completed":
                return "repeat"
            if result.status != "waiting":
                return "call_agent" if result.status != "cancelled" else "unknown"
            await asyncio.sleep(supervision_module.FACT_FRAME_INTERVAL_S)
        return "unknown"

    async def _judge_terminal_frame(self, *args: Any, **kwargs: Any) -> str:
        """Submit a terminal question to the single lifecycle consumer.

        Callers retain settlement/history and their existing timeout budget.
        They never open a parallel Decision request while waiting for a result.
        """
        await self.start()
        answer = asyncio.get_running_loop().create_future()
        self._terminal_questions.append((answer, args, kwargs))
        self._loop_wakeup.set()
        try:
            return await answer
        finally:
            if not answer.done():
                answer.cancel()
            self._loop_wakeup.set()

    async def _serve_terminal_question(self, answer, args, kwargs) -> None:
        """The lifecycle schedules at most one request, including terminals."""
        if answer.done():
            return
        request = asyncio.create_task(self._judge_frame(*args, **kwargs))
        wake = None
        try:
            while not answer.done():
                self._loop_wakeup.clear()
                wake = asyncio.create_task(self._loop_wakeup.wait())
                done, _ = await asyncio.wait({request, answer, wake}, return_when=asyncio.FIRST_COMPLETED)
                if wake not in done:
                    wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                wake = None
                if self._lifecycle_events and await self._process_lifecycle_events():
                    if not answer.done():
                        answer.set_result("unknown")
                    break
                if answer.done():
                    break
                if request in done:
                    answer.set_result(request.result())
                    break
        except asyncio.CancelledError:
            answer.cancel()
            raise
        except Exception as exc:
            if not answer.done():
                answer.set_exception(exc)
        finally:
            if wake is not None:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def _judge_frame(self, *args: Any, **kwargs: Any) -> str:
        if await self._process_lifecycle_events():
            return "unknown"
        death_count = self._death_count
        choice = await super()._judge_frame(*args, **kwargs)
        if death_count != self._death_count or await self._process_lifecycle_events():
            return "unknown"
        return choice

    @staticmethod
    def _remaining(record: Mapping[str, Any]) -> float | None:
        timeout = record.get("timeout_s")
        if timeout is None:
            return None
        return max(0.0, float(record.get("started_at", time.monotonic())) + float(timeout) - time.monotonic())

    async def _protect_work(self, record, execution, choice, action) -> None:
        """Execution waits outside the lifecycle; the original deadline remains authoritative."""
        try:
            remaining = self._remaining(record)
            if remaining is None:
                await self._run_default_action(record, execution, choice, action)
            else:
                await asyncio.wait_for(self._run_default_action(record, execution, choice, action), remaining)
        except asyncio.TimeoutError:
            self._refresh_interrupted_result(record)
            safe = self._handoff_facts(execution, reason="decision_default_tool",
                                       confirmed=False, decision="unknown")
            safe["default_action"] = {"choice": choice, "tool": action[0],
                                      "result": self._compact_result(record.get("protective_result", {})),
                                      "then": "unknown"}
            safe["history"] = copy.deepcopy(record.get("history", []))
            safe["history_earlier"] = int(record.get("history_total", 0)) - len(record.get("history", []))
            self.runtime.direct_call_handoffs[record["call_id"]] = {"facts": safe, "settlement": {"settled": True}}
            self.runtime.trace.emit("call_supervision_default_deadline", tool_call_id=record["call_id"], facts=safe)
        finally:
            self._suspended = None
            self._loop_wakeup.set()

    async def _execute_protection(self, choice, action, record=None, *, call_id=None):
        """Register each protective action as current work in the same lifecycle.

        Switching responses replaces the current action, never the original
        suspended slot. Only the enclosing transaction can notify the Agent.
        """
        from minebot.brain.registry import execute_tool
        while True:
            self._protective_redirect = None
            self._protective_verdict = None
            default_id = call_id or f"reflex-default-{uuid4()}"
            default_tool = self.runtime.registry.get(action[0])
            if getattr(self, "_reflex_claim_holder", "threat") == "life":
                default_tool = self._life_tool(default_tool)
            chain = self._reaction_selection
            self._reaction_selection = None
            if chain is not None and chain["choice"] == choice:
                self._reaction_actions[default_id] = {**chain, "tool": action[0]}
            try:
                result = await self.runtime.run_tool(
                    default_tool, execute_tool, default_tool, action[1],
                    getattr(self.runtime, "weld_context", None),
                    tool_call_id=default_id, claim_holder=getattr(self, "_reflex_claim_holder", "threat"),
                    supervision_context={"is_reflex_action": True,
                                         "is_life_action": getattr(self, "_reflex_claim_holder", "threat") == "life",
                                         "reflex_action_choice": choice,
                                         "intent": getattr(self, "_reflex_action_intent", None)
                                         or supervision_module._DEFAULT_ACTION_TEXT.get(choice, choice)},
                )
            except Exception as exc:
                result = {"success": False, "reason": type(exc).__name__}
                self.runtime.trace.emit("call_supervision_default_failed", tool_call_id=default_id,
                                        choice=choice, error_type=type(exc).__name__)
            incomplete = self._reaction_actions.pop(default_id, None)
            if incomplete is not None:
                self.runtime.trace.emit("reflex_reaction_chain", **incomplete,
                                        tool_call_id=default_id, first_native_progress=False,
                                        result=self._compact_result(result))
            if record is not None:
                record["protective_result"] = result
                record["protective_action"] = (choice, action)
                self._refresh_interrupted_result(record)
            redirect = self._protective_redirect
            if redirect is None:
                return choice, action, result, default_id
            choice, action = redirect
            if choice in rq.PROTECTIVE:
                self._reflex_claim_holder = "threat"
                self._reflex_action_intent = None
            call_id = None

    def _life_tool(self, tool):
        """Life movement may not edit terrain. Scope existing Body settings
        inside the admitted writer operation and restore them after settlement.
        This does not introduce another execution lane or stop entrance.
        """
        body = self.runtime.body
        def settings(values):
            action = Action.create("setBaritoneSettings", values)
            accepted = body.execute(action)
            if not accepted.ok:
                return False
            if getattr(accepted, "complete", True):
                return True
            # Native acceptance precedes application. Do not start movement
            # until the existing control terminal confirms these settings.
            terminal = body.await_action_terminal(action.id, timeout_s=tool.sidecar.timeout_s or FACT_FRAME_INTERVAL_S)
            return terminal.data.get("success") is True
        def execute(params):
            state = body.get_reflex_facts()
            previous = {key: state[key] for key in ("allow_break", "allow_place") if isinstance(state.get(key), bool)}
            if len(previous) != 2:
                return ToolResult(False, "life_movement_permissions_unknown", True)
            if not settings({"allow_break": False, "allow_place": False}):
                return ToolResult(False, "life_movement_permissions_rejected", True)
            try:
                return tool.callable(params)
            finally:
                if not settings(previous):
                    self.runtime.trace.emit("reflex_life_settings_restore_failed", tool=tool.name)
        return replace(tool, callable=execute)

    async def _run_idle_action(self, choice: str, facts: Mapping[str, Any]) -> None:
        """Run one reversible life-profile action through the shared Body loop."""
        action = self._default_action(choice, {"facts": dict(facts)})
        if action is None:
            return
        self.runtime.trace.emit(
            "call_supervision_default_requested", tool_call_id="idle-loop",
            choice=choice, tool=action[0], arguments=action[1], loop_state=self.loop_state,
        )
        previous_holder = getattr(self, "_reflex_claim_holder", "threat")
        previous_intent = getattr(self, "_reflex_action_intent", None)
        self._reflex_claim_holder = "life"
        self._reflex_action_intent = {
            "go_home": "Walk to the home position.",
            "approach_player": "Walk to the nearby player.",
            "follow_player": "Follow the nearby player.",
            "look_player": "Look toward the nearby player.",
            "seek_player": "Find the player named in the life profile.",
        }.get(choice, choice)
        try:
            choice, action, result, call_id = await self._execute_protection(
                choice, action, call_id=f"reflex-life-{self._idle_sequence}")
            if self._protective_verdict == "call_agent":
                notify = getattr(self.runtime, "notify_reflex_fact", None)
                if callable(notify):
                    notify({"call_id": call_id, "status": "handoff_requested",
                            "reason": "decision_call_agent", "result": self._compact_result(result)})
            else:
                self._remember_protection(action, result)
            self.runtime.trace.emit(
                "call_supervision_default_finished", tool_call_id="idle-loop",
                choice=choice, tool=action[0], result=self._compact_result(result),
            )
        finally:
            self._reflex_claim_holder = previous_holder
            self._reflex_action_intent = previous_intent
            self._set_state("idle", "call_ended")
            self._loop_wakeup.set()

    def _refresh_interrupted_result(self, record) -> None:
        """Keep actual terminals when settlement and the caller resume separately."""
        outcome = record.get("interrupted_result")
        if not isinstance(outcome, dict):
            return
        interruption = outcome["metrics"]["direct_call_handoff"]
        original = record.get("result")
        if isinstance(original, Mapping):
            outcome.clear()
            outcome.update(self._compact_result(original))
            outcome["metrics"] = {"direct_call_handoff": interruption}
        protective = record.get("protective_action")
        if protective is not None:
            choice, action = protective
            interruption["facts"]["default_action"].update(
                choice=choice, tool=action[0],
                result=self._compact_result(record.get("protective_result", {})))

    def _remember_protection(self, action, result) -> None:
        facts = self._idle_facts()
        # Native action terminals use ``position``; the Agent's cached state
        # may still be its read before the protective movement.
        metrics = result.get("metrics") if isinstance(result, Mapping) else None
        if isinstance(metrics, Mapping):
            position = metrics.get("position", metrics.get("pos"))
            if isinstance(position, (list, tuple)) and len(position) == 3:
                facts["position"] = list(position)
        self.record_experience(rq.protection_experience(time.time(), action[0], action[1],
                                                       self._compact_result(result), facts))

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
        original = record.get("result")
        original = original if isinstance(original, Mapping) else {"success": False, "reason": "stopped_by_decision"}
        outcome = self._compact_result(original)
        outcome["metrics"] = {"direct_call_handoff": {"facts": {
            "reason": "decision_default_tool", "decision": {"choice": choice},
            "default_action": {"choice": choice, "tool": action[0]},
        }}}
        record["interrupted_result"] = outcome
        record.pop("protective_action", None)
        record.pop("protective_result", None)
        body = execution.get("facts") or {}
        history = record.setdefault("history", [])
        history.append({"result": outcome, "body": {
            "position": body.get("position", body.get("pos")),
            "inventory": body.get("inventory", body.get("inventory_counts")),
        }})
        record["history_total"] = int(record.get("history_total") or 0) + 1
        del history[:-5]
        choice, action, default_result, default_id = await self._execute_protection(choice, action, record)
        record["protective_result"] = default_result
        record["protective_action"] = (choice, action)
        self._refresh_interrupted_result(record)
        outcome["metrics"]["direct_call_handoff"]["facts"]["default_action"].update(
            choice=choice, tool=action[0], result=self._compact_result(default_result))
        after = await self._after_default_action(
            record, action, default_result, default_id, protective_choice=choice,
        )
        self._refresh_interrupted_result(record)
        choice, action = record.get("protective_action", (choice, action))
        default_result = record.get("protective_result", default_result)
        handoff = {**safe, "default_action": {
            "choice": choice, "tool": action[0], "result": self._compact_result(default_result),
            "then": after,
        }, "settlement": settled}
        # Reuse the existing handoff facts rather than adding tool receipt
        # fields. The Agent and next node attempt receive the same raw record.
        handoff["history"] = copy.deepcopy(history)
        handoff["history_earlier"] = record["history_total"] - len(history)
        # The plan runtime reads this choice: "repeat" re-enters the stopped node.
        handoff["decision"] = {**dict(handoff.get("decision") or {}),
                               "choice": "repeat" if after == "resume" else after}
        self.runtime.direct_call_handoffs[call_id] = {"facts": handoff, "settlement": settled}
        self.runtime.trace.emit(
            "call_supervision_default_finished", tool_call_id=call_id,
            choice=choice, facts=handoff,
        )
        if after == "resume":
            self._set_state("work", "decision")
            # Back to the earlier work; the Agent is not woken for this.
            record["resume_original"] = True
            self._remember_protection(action, default_result)
        elif after == "call_agent" and not bool(record["supervision_context"].get("is_plan")):
            self._set_state("idle", "decision")
            notify_fact = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(notify_fact):
                notify_fact({"call_id": call_id, "status": "default_action_completed", **handoff})

    async def _after_default_action(
        self, record: dict[str, Any], action: tuple[str, dict[str, Any]],
        default_result: Any, default_id: str, *, protective_choice: str = "",
    ) -> str:
        # The Body remains in its protective result state during unknown
        # retries. Reserve the existing result slot for the entire transaction,
        # including the cadence between requests, so idle never consumes a
        # result answer or starts unrelated work in that gap.
        call_id = str(record["call_id"])
        reserved_here = call_id not in self._result_pending
        if reserved_here:
            self._result_pending[call_id] = record
        try:
            return await self._follow_default_result(
                record, action, default_result, default_id, protective_choice=protective_choice)
        finally:
            if reserved_here:
                self._release_result_pending(call_id)

    async def _follow_default_result(
        self, record: dict[str, Any], action: tuple[str, dict[str, Any]],
        default_result: Any, default_id: str, *, protective_choice: str = "",
    ) -> str:
        """Ask what follows a protective action: go back, do it again, or the Agent."""
        if self._protective_verdict == "call_agent":
            return "call_agent"
        context = record["supervision_context"]
        intent = str(context.get("intent") or context.get("node_description") or "")
        registry = getattr(self.runtime, "registry", None)
        default_tool = registry.get(action[0]) if registry is not None else None
        history: list[dict[str, Any]] = []
        total = 0
        new_attempt = True
        while default_tool is not None:
            self._refresh_interrupted_result(record)
            if await self._process_lifecycle_events():
                return "unknown"
            remaining = self._remaining(record)
            if remaining is not None and remaining <= 0:
                return "unknown"
            payload = default_result if isinstance(default_result, Mapping) else getattr(default_result, "result", default_result)
            if not isinstance(payload, Mapping):
                payload = {"success": False, "reason": str(payload)[:200]}
            facts = await self._read_idle_facts()
            execution = {"tool": action[0], "state": "succeeded" if payload.get("success") is True else "failed",
                         "facts": facts, "result": dict(payload)}
            if new_attempt:
                history.append({"result": self._compact_result(payload),
                                "body": {"position": facts.get("position", facts.get("pos")),
                                         "inventory": facts.get("inventory", facts.get("inventory_counts"))}})
                total += 1
                del history[:-5]
                new_attempt = False
            options = rq.after_default_options(
                original_call="" if context.get("idle_protection") else rq.call_text(record["tool"], record["params"]), intent=intent,
                default_call=rq.call_text(action[0], action[1]), result=payload,
                facts=execution["facts"],
            )
            try:
                choice = await asyncio.wait_for(self._judge_terminal_frame(
                    record["call_id"], default_tool, action[1], execution, 0.0,
                    purpose="default_result_frame", supervision_context=context, options=options,
                    decision_context={"tool_result": dict(payload), "tool_arguments": dict(action[1]),
                                      "body_state": execution["facts"], "history": list(history),
                                      "history_earlier": total - len(history),
                                      "interrupted_history": copy.deepcopy(record.get("history", [])),
                                      "interrupted_history_earlier": int(record.get("history_total", 0)) - len(record.get("history", []))},
                ), timeout=2.2 if remaining is None else min(2.2, remaining))
            except asyncio.TimeoutError:
                choice = "unknown"
            record["questions"] += 1
            if choice in {"resume", "stay", "call_agent"}:
                return choice
            candidates = rq.equipment_candidates(execution["facts"], payload)
            if choice in candidates:
                # A protective call is a Body call too. Reuse the existing
                # judged equipment node, then retry this protective call;
                # resuming the suspended work is still a later choice.
                choice = await self._run_result_equipment(
                    record, default_tool, action[1], candidates[choice], claim_holder="threat",
                    reflex_action_choice=protective_choice)
                if choice == "call_agent":
                    return choice
            if choice != "repeat":
                # A missing/illegal/late answer is no choice. Keep the
                # interrupted work pending, refresh facts at the next cadence,
                # and do not wake the Agent or add a fictitious tool attempt.
                record["unknown_answers"] = int(record.get("unknown_answers") or 0) + 1
                remaining = self._remaining(record)
                await asyncio.sleep(FACT_FRAME_INTERVAL_S if remaining is None else min(FACT_FRAME_INTERVAL_S, remaining))
                continue
            protective_choice, action, default_result, default_id = await self._execute_protection(
                protective_choice, action, record, call_id=f"{default_id}-{total}")
            default_tool = registry.get(action[0])
            if self._protective_verdict == "call_agent":
                return "call_agent"
            new_attempt = True
        return "unknown"


    def _set_state(self, state: str, cause: str) -> None:
        if self.loop_state == "dead" and cause != "respawned":
            return
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
        question: Mapping[str, Any] | None = None,
    ) -> str:
        """Judge a plan call's terminal fact through this Body's loop.

        WorkPlanExecutor supplies the compiled terminal question and tree
        range. The Body loop owns the Decision channel and preserves that
        question rather than compiling a different version. Unknown stays
        unknown and lets the same loop ask again while the Body is idle.
        """
        result = decision_context.get("tool_result")
        result = result if isinstance(result, Mapping) else {}
        facts = decision_context.get("body_state")
        facts = facts if isinstance(facts, Mapping) else {}
        if await self._process_lifecycle_events(facts):
            return "unknown"
        execution = decision_context.get("execution")
        execution = dict(execution) if isinstance(execution, Mapping) else {}
        execution.setdefault("tool", getattr(tool, "name", "unknown"))
        execution["state"] = "succeeded" if result.get("success") is True else "failed"
        execution["facts"] = dict(facts)
        options = None
        if question is None:
            options = rq.frame_options(
                tool=str(getattr(tool, "name", "unknown")), params=params,
                intent=str(plan_context.get("intent") or ""), facts=facts,
                running=False, result=result, next_call=next_call,
                repeat_note=repeat_note,
            )
            options = {key: value for key, value in options.items()
                       if key not in rq.PROTECTIVE and key != "inspect"}
        try:
            return await self._judge_terminal_frame(
                call_id, tool, params, execution, 0.0,
                purpose="plan_result_frame", supervision_context=plan_context,
                options=options, decision_context=decision_context,
                question=question,
            )
        finally:
            self._release_result_pending(str(call_id))

    def _release_result_pending(self, call_id: str) -> None:
        # Plan calls enter the shared runtime with the transport id
        # ``plan-{node_id}``, while the result judge is deliberately passed
        # the tree's node id.  Release both spellings so a judged plan result
        # cannot leave the lifecycle loop permanently waiting for a slot.
        value = str(call_id)
        keys = {value, f"plan-{value}"}
        if value.startswith("plan-"):
            keys.add(value[5:])
        for key in keys:
            self._result_pending.pop(key, None)
        self._loop_wakeup.set()

    async def start(self) -> None:
        """Start the one lifecycle task when the SDK session enters the world."""
        if self._lifecycle_started:
            return
        self._lifecycle_started = True
        loop = asyncio.get_running_loop()
        self._reaction_event_loop = loop
        body = getattr(self.runtime, "body", None)
        register = getattr(body, "add_lifecycle_listener", None)
        if callable(register):
            def receive(event):
                if not loop.is_closed():
                    loop.call_soon_threadsafe(self._queue_lifecycle_event, event)
            self._body_lifecycle_listener = receive
            self._native_lifecycle = True
            register(receive)
        self.runtime.trace.emit("reflex_loop_started", loop_state=self.loop_state)
        self._lifecycle_task = asyncio.create_task(self._idle_frames())

    def _ensure_idle_task(self) -> None:
        # The lifecycle task never ends after a call; it switches back to its
        # idle branch once the registered monitor signals completion.
        if self._lifecycle_started and self._lifecycle_task is not None:
            self._loop_wakeup.set()

    async def stop(self) -> None:
        """Stop the lifecycle task before the runtime closes its read lane."""
        task = self._lifecycle_task
        remove = getattr(getattr(self.runtime, "body", None), "remove_lifecycle_listener", None)
        if callable(remove) and self._body_lifecycle_listener is not None:
            remove(self._body_lifecycle_listener)
        self._body_lifecycle_listener = None
        self._lifecycle_task = None
        active = self._active_monitor
        self._active_monitor = None
        self._result_pending.clear()
        while self._terminal_questions:
            answer, _, _ = self._terminal_questions.popleft()
            answer.cancel()
        if active is not None:
            active[0]["cancel_requested"] = True
            done = active[0].get("monitor_done")
            if isinstance(done, asyncio.Event):
                done.set()
        self._loop_wakeup.set()
        protection = self._protection_task
        self._protection_task = None
        if protection is not None and not protection.done():
            protection.cancel()
            await asyncio.gather(protection, return_exceptions=True)
        self._suspended = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._lifecycle_started:
            self.runtime.trace.emit("reflex_loop_stopped", loop_state=self.loop_state)
        self._lifecycle_started = False
        self._ambient_facts.clear()

    def _idle_facts(self) -> dict[str, Any]:
        cached = getattr(self.runtime, "last_known_body_state", None)
        if isinstance(cached, Mapping):
            return dict(cached)
        return {}

    async def _read_body_facts(self) -> dict[str, Any]:
        """Read raw state and perception on the same lane in work and idle."""
        body = getattr(self.runtime, "body", None)
        getter = getattr(body, "get_reflex_facts", None) or getattr(body, "get_state", None)
        read_lane = getattr(self.runtime, "_read_lane", None)
        facts = self._idle_facts()
        if callable(getter) and read_lane is not None:
            try:
                value = await read_lane.run(getter)
                if isinstance(value, Mapping):
                    facts = dict(value)
                elif is_dataclass(value):
                    facts = asdict(value)
                else:
                    raw = getattr(value, "__dict__", None)
                    if isinstance(raw, dict):
                        facts = dict(raw)
            except Exception as exc:
                self.runtime.trace.emit(
                    "reflex_idle_read_unknown", scope="state", error_type=type(exc).__name__
                )
            # BodyState intentionally projects state, not nearby entities.
            # Use the existing perception API rather than adding BodyState or
            # tool-result fields. An entity read gap must not discard a state
            # read that already succeeded.
            perceive = getattr(body, "perceive", None)
            if callable(perceive) and not any(
                    isinstance(facts.get(key), (list, tuple))
                    for key in ("entities", "nearby_entities", "hostiles", "nearby_hostiles")):
                try:
                    nearby = await read_lane.run(perceive, "nearbyEntities", {"radius": 64, "limit": 32})
                    ok = nearby.get("ok") if isinstance(nearby, Mapping) else getattr(nearby, "ok", False)
                    data = nearby.get("data") if isinstance(nearby, Mapping) else getattr(nearby, "data", None)
                    if ok is True and isinstance(data, Mapping) and isinstance(data.get("entities"), list):
                        facts["nearby_entities"] = list(data["entities"])
                    else:
                        self.runtime.trace.emit("reflex_idle_read_unknown", scope="nearbyEntities")
                except Exception as exc:
                    self.runtime.trace.emit(
                        "reflex_idle_read_unknown", scope="nearbyEntities", error_type=type(exc).__name__
                    )
        if self._life_profile:
            facts["life_profile"] = copy.deepcopy(self._life_profile)
            facts["life_intent"] = life_intent(self._life_profile)
        return facts

    async def _read_idle_facts(self) -> dict[str, Any]:
        return self._frame({"facts": await self._read_body_facts()})["facts"]

    async def _refresh_running_facts(self, call_id: str) -> None:
        body = getattr(self.runtime, "body", None)
        getter = getattr(body, "get_reflex_facts", None) or getattr(body, "get_state", None)
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        observe = getattr(snapshots, "observe", None)
        if (callable(getter) and getattr(self.runtime, "_read_lane", None) is not None
                and callable(observe)):
            # observe already rejects late reads after a terminal or a change
            # of call ownership. Do not write enriched history back as raw.
            facts = await self._read_body_facts()
            # The action observer owns its navigation, phase and movement.
            # This read supplies the facts missing from lite progress, not a
            # second execution progress stream. Explicit complete inventory
            # remains a complete observation even in this lite envelope.
            ambient_keys = ("inventory", "inventory_counts", "inventory_complete",
                "inventory_includes_selected", "selected_item", "selected_item_known",
                "entities", "nearby_entities", "hostiles", "nearby_hostiles",
                "health", "food", "oxygen", "head_in_liquid", "on_ground")
            observe(call_id, {"lite": True, **{key: facts[key] for key in ambient_keys if key in facts}})

    @staticmethod
    def _idle_options(facts: Mapping[str, Any]) -> dict[str, str]:
        return rq.idle_options(facts)

    def _idle_question(self, facts: Mapping[str, Any], options: Mapping[str, str]) -> dict[str, Any]:
        return rq.idle_question(facts, options)

    async def _run_idle_protection(self, choice: str, facts: Mapping[str, Any]) -> None:
        """Execute one reversible idle protection action through run_tool."""
        action = self._default_action(choice, {"facts": dict(facts)})
        if action is None:
            return
        default_tool = self.runtime.registry.get(action[0])
        timeout = action[1].get("timeout_s", getattr(getattr(default_tool, "sidecar", None), "timeout_s", None))
        record = {"call_id": f"reflex-idle-{self._idle_sequence}", "tool": action[0], "params": dict(action[1]),
                  "started_at": time.monotonic(), "timeout_s": timeout, "questions": 0,
                  "supervision_context": {"goal": "", "idle_protection": True, "is_reflex_action": True,
                      "intent": supervision_module._DEFAULT_ACTION_TEXT.get(choice, choice)}}
        self.runtime.trace.emit(
            "call_supervision_default_requested", tool_call_id="idle-loop",
            choice=choice, tool=action[0], arguments=action[1], loop_state=self.loop_state,
        )
        try:
            choice, action, result, call_id = await self._execute_protection(
                choice, action, record, call_id=record["call_id"])
            after = "call_agent" if self._protective_verdict == "call_agent" else "unknown"
            if after != "call_agent":
                remaining = self._remaining(record)
                if remaining is None or remaining > 0:
                    try:
                        after = await asyncio.wait_for(
                            self._after_default_action(record, action, result, call_id, protective_choice=choice),
                            timeout=remaining)
                    except asyncio.TimeoutError:
                        after = "unknown"
                choice, action = record.get("protective_action", (choice, action))
                result = record.get("protective_result", result)
            if after == "call_agent":
                notify = getattr(self.runtime, "notify_reflex_fact", None)
                if callable(notify):
                    notify({"call_id": call_id, "status": "handoff_requested",
                            "reason": "decision_call_agent", "result": self._compact_result(result)})
            else:
                self._remember_protection(action, result)
            self.runtime.trace.emit("call_supervision_default_finished", tool_call_id="idle-loop",
                                    choice=choice, tool=action[0], result=self._compact_result(result))
        finally:
            self._set_state("idle", "call_ended")
            self._loop_wakeup.set()

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
                await self._process_lifecycle_events()
                active = self._active_monitor
                if active is not None:
                    record, tool, params, wake, worker = active
                    try:
                        await self._monitor(record, tool, params, wake, worker)
                    finally:
                        if self._active_monitor is active:
                            self._active_monitor = None
                        if not record.get("result_abandoned"):
                            self._result_pending[str(record.get("call_id") or "")] = record
                        done = record.get("monitor_done")
                        if isinstance(done, asyncio.Event):
                            done.set()
                    continue

                if self._terminal_questions:
                    answer, args, kwargs = self._terminal_questions.popleft()
                    await self._serve_terminal_question(answer, args, kwargs)
                    continue

                self._loop_wakeup.clear()
                try:
                    await asyncio.wait_for(self._loop_wakeup.wait(), timeout=FACT_FRAME_INTERVAL_S)
                    continue
                except asyncio.TimeoutError:
                    pass
                if self._active_monitor is not None:
                    continue
                if self._result_pending or (self._protection_task is not None and not self._protection_task.done()):
                    # The worker has ended, but the Agent/plan result judge is
                    # still using the Body facts.  Do not start idle work or
                    # ask an idle Decision question until it releases this
                    # result slot.
                    continue
                snapshots = getattr(self.runtime, "execution_snapshots", None)
                if snapshots is not None and snapshots.current() is not None:
                    continue
                facts = await self._read_idle_facts()
                if await self._process_lifecycle_events(facts):
                    continue
                # An Agent call can arrive while the read lane is sampling.
                if self._active_monitor is not None or self._result_pending:
                    continue
                options = self._idle_options(facts)
                request = self._idle_question(facts, options)
                self._idle_sequence += 1
                started = time.monotonic()
                reaction = self._reaction_question(facts, started)
                self.runtime.trace.emit(
                    "call_supervision_request", tool_call_id="idle-loop",
                    tool="__idle__", purpose="idle_frame", loop_state="idle",
                    fact_revision=self._idle_sequence, request=request,
                    request_chars=len(str(request)),
                    frame_input=getattr(self, "_last_frame_input", {"execution": {"facts": dict(facts)}}),
                    compiler_input={"facts": dict(facts), "options": dict(options)},
                    clock={"request_monotonic_s": started},
                )
                answer_task = wake_task = None
                try:
                    # Idle is a live state, so still ask even when the only
                    # reversible normal option is ``stay``.  This keeps the
                    # same one-question-per-frame contract as a running call.
                    answer_task = asyncio.create_task(self._client.ask(request))
                    wake_task = asyncio.create_task(self._loop_wakeup.wait())
                    done, _ = await asyncio.wait(
                        {answer_task, wake_task}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if wake_task in done:
                        # Even a completed answer belongs to the superseded
                        # idle frame when work arrived in the same wait cycle.
                        choice = "unknown"
                    else:
                        answer = await answer_task
                        choice = rq.frame_choice(answer.raw, request["questions"])
                    if wake_task not in done:
                        wake_task.cancel()
                    await asyncio.gather(wake_task, return_exceptions=True)
                except Exception:
                    choice = "unknown"
                finally:
                    children = [child for child in (answer_task, wake_task) if child is not None]
                    for child in children:
                        if not child.done():
                            child.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
                self.runtime.trace.emit(
                    "call_supervision_result", tool_call_id="idle-loop",
                    tool="__idle__", purpose="idle_frame", choice=choice,
                    unknown=choice == "unknown",
                    decision_ms=round((time.monotonic() - started) * 1000.0, 3),
                )
                self._reaction_answer(reaction, choice)
                if choice in rq.PROTECTIVE or choice == "eat":
                    self._protection_task = asyncio.create_task(self._run_idle_protection(choice, facts))
                elif choice in {"go_home", "approach_player", "follow_player", "look_player", "seek_player"}:
                    self._protection_task = asyncio.create_task(self._run_idle_action(choice, facts))
                elif choice == "call_agent":
                    notify = getattr(self.runtime, "notify_reflex_fact", None)
                    if callable(notify):
                        notify({"call_id": "idle-loop", "status": "handoff_requested",
                                "reason": "decision_call_agent", "facts": dict(facts)})
        except asyncio.CancelledError:
            raise


__all__ = ["BodyLoop"]
