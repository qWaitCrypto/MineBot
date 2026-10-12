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
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from minebot.app import reflex_question as rq
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

    async def _idle_frames(self) -> None:
        """Produce idle facts at the same cadence as running frames.

        This skeleton deliberately only reports the finite choice.  Protective
        action execution is added by the survival slice; no write is started by
        an unknown or an idle answer in Step 2.
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
        except asyncio.CancelledError:
            raise


__all__ = ["BodyLoop"]
