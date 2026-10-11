"""Default supervision for every long-running Body call.

The supervisor is shared by direct Agent calls and work-plan call nodes.  It
reads the execution snapshot, asks Decision only when the snapshot has stopped
providing real progress, and uses the existing unified stop/settlement path.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from minebot.app.decision_client import DecisionClient, DecisionPriority
from minebot.contract.execution import tool_observation_scope

MAX_SUPERVISION_READS = 4
MAX_SUPERVISION_QUESTIONS = 4
# Keep the common short-call path's scheduling contract close to the existing
# direct execution lane.  A call that survives this grace period is a long
# call and receives the monitor below.
FAST_COMPLETION_GRACE_S = 0.02
DEFAULT_TOOL_EXPLANATION = (
    "The call normally advances through native Body evidence and eventually "
    "returns one authoritative terminal result. Navigation, phase changes, "
    "world edits and inventory changes are progress; a live process, heartbeat "
    "or repeated sample is not."
)
CRITERIA = {
    "continue": "The supplied execution evidence supports normal ongoing work; keep the admitted call running.",
    "inspect": "The evidence is incomplete or unclear; request one bounded read before judging again.",
    "yield": "The supplied evidence supports handing control back after the call is stopped and settled.",
}


class CallSupervisor:
    """One rule for all running mutating calls.

    ``run`` is invoked from ``AgentRuntime.run_tool`` for both ``agent_call``
    and ``agent_plan``. It never owns Body control: the runtime's existing
    claim and unified cancellation entry remain authoritative.
    """

    def __init__(self, runtime: Any, *, decision_url: str | None = None,
                 suspicion_s: float = 5.0, review_interval_s: float = 15.0,
                 max_reads: int = MAX_SUPERVISION_READS,
                 max_questions: int = MAX_SUPERVISION_QUESTIONS) -> None:
        self.runtime = runtime
        self.decision_url = decision_url or os.environ.get(
            "MINEBOT_DECISION_URL", "http://117.176.14.165:10004/v1/systemone"
        )
        self.suspicion_s = suspicion_s
        self.review_interval_s = review_interval_s
        self.max_reads = max_reads
        self.max_questions = max_questions
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
            return value.strip()
        description = getattr(tool, "description", None)
        return str(description).strip() if description else DEFAULT_TOOL_EXPLANATION

    def _snapshot(self, call_id: str) -> dict[str, Any]:
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        current = snapshots.current() if snapshots is not None else None
        if isinstance(current, dict) and current.get("operation_id") == call_id:
            return current
        return {}

    async def judge(self, call_id: str, tool: Any, params: Mapping[str, Any],
                    execution: Mapping[str, Any], idle_s: float) -> str:
        request = {
            "state": {
                "protocol": "minebot-call-supervision/2",
                "tool": {
                    "name": getattr(tool, "name", "unknown"),
                    "arguments": dict(params),
                    "normal_work": self._explanation(tool),
                },
                "execution": dict(execution),
                "no_explainable_progress_s": round(idle_s, 2),
                "evidence_scope": (
                    "Only the execution snapshot and the registered normal-work explanation are known. "
                    "Omitted facts are unknown. Do not infer a physical result from process liveness."
                ),
            },
            "questions": {"q": {"type": "choice",
                "instructions": "Judge the active call using only these facts.",
                "criteria": CRITERIA}},
        }
        tool_name = getattr(tool, "name", "unknown")
        self.runtime.trace.emit("call_supervision_request", tool_call_id=call_id,
                                tool=tool_name, purpose="supervision",
                                idle_s=round(idle_s, 3), request=request)
        # Keep slice-1 replay event names in addition to the shared event.
        self.runtime.trace.emit("direct_supervision_request", tool_call_id=call_id,
                                regression_format="minebot-semantic-regression/1",
                                request=request)
        started = time.monotonic()
        raw: dict[str, Any] | None = None
        try:
            answer = await self._client.ask(request, priority=DecisionPriority.SUPERVISION)
            raw = answer.raw
            choice = answer.choice if answer.choice in CRITERIA else "unknown"
        except Exception:
            # Transport errors, timeouts and malformed answers are unknown and
            # never stop Body work on their own.
            choice = "unknown"
        elapsed = time.monotonic() - started
        self.runtime.trace.emit("call_supervision_result", tool_call_id=call_id,
                                tool=tool_name, purpose="supervision", response=raw,
                                choice=choice, unknown=choice == "unknown",
                                elapsed_s=elapsed)
        self.runtime.trace.emit("direct_supervision_result", tool_call_id=call_id,
                                response=raw, choice=choice, elapsed_s=elapsed)
        return choice

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
            self.runtime.trace.emit("call_supervision_read_unknown",
                                    tool_call_id=record["call_id"], tool=getattr(tool, "name", "unknown"),
                                    error_type=type(exc).__name__)
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
        self.runtime.trace.emit("call_supervision_read", tool_call_id=record["call_id"],
                                tool=getattr(tool, "name", "unknown"),
                                read_number=record["reads"], facts=facts)
        return facts

    @staticmethod
    def _deterministic_terminal(execution: Mapping[str, Any], read: Mapping[str, Any]) -> dict[str, Any] | None:
        """Keep the existing construction terminal evidence intact.

        The supervisor does not select this by tool name. A registered bounded
        reader may expose a complete, non-empty remainder and the snapshot may
        prove the native builder is paused with a complete inventory read. No
        equivalent evidence is inferred for other calls.
        """
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
        if inventory.get(item, 0) != 0 or not read.get("remaining_cells"):
            return None
        # Do not put the material name or a remedy in the Agent handoff.
        return {"reason": "material_exhausted", "confirmed": True, "construction": read}

    @staticmethod
    def _handoff_facts(execution: Mapping[str, Any], *, reason: str,
                       confirmed: bool, decision: str | None,
                       extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        facts = execution.get("facts") if isinstance(execution.get("facts"), Mapping) else {}
        evidence = {
            key: facts.get(key) for key in
            ("position", "navigation", "pathing", "processes", "builder_paused", "goal")
            if key in facts
        }
        construction = facts.get("construction")
        if isinstance(construction, Mapping):
            evidence["construction"] = {
                key: value for key, value in construction.items()
                if key != "material_item"
            }
        safe: dict[str, Any] = {
            "reason": reason, "confirmed": confirmed,
            "what": {"tool": execution.get("tool"), "phase": execution.get("phase"),
                      "elapsed_s": execution.get("elapsed_s"),
                      "no_progress_s": execution.get("since_progress_s"),
                      "last_progress_at": execution.get("last_progress_at"),
                      "progress_signal": execution.get("progress_signal")},
            "body": {"position": facts.get("position")},
            "decision": {"choice": decision, "evidence": evidence},
        }
        if isinstance(extra, Mapping) and isinstance(extra.get("construction"), Mapping):
            construction = dict(extra["construction"])
            construction.pop("material_item", None)
            safe["construction"] = construction
        return safe

    async def run(self, tool: Any, callback: Any, args: tuple[Any, ...], *,
                  call_id: str, timeout_s: float | None) -> Any:
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        record: dict[str, Any] = {"call_id": call_id, "action_id": None,
                                  "cancel_requested": False, "settling": False,
                                  "reads": 0, "questions": 0}
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
            loop.call_soon_threadsafe(receive, dict(payload))

        def invoke() -> Any:
            if snapshots is not None:
                snapshots.running(call_id)
            with tool_observation_scope(call_id, notify):
                return callback(*args)

        worker = asyncio.create_task(self.runtime.execution_lane.run(invoke, timeout_s=timeout_s))
        params = args[1] if len(args) > 1 and isinstance(args[1], Mapping) else {}
        monitor: asyncio.Task[Any] | None = None
        try:
            # Fast terminal calls should retain the old lane timing.  This is
            # only a scheduling grace period; every call that remains active
            # after it enters the shared monitor.
            grace = min(FAST_COMPLETION_GRACE_S, max(0.0, self.suspicion_s))
            if grace:
                done, _ = await asyncio.wait({worker}, timeout=grace)
                if not done:
                    monitor = asyncio.create_task(self._monitor(record, tool, params, wake, worker))
            else:
                monitor = asyncio.create_task(self._monitor(record, tool, params, wake, worker))
            result = await worker
            if record["settling"] and monitor is not None:
                await monitor
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
            self.runtime.trace.emit("call_supervision_finished", tool_call_id=call_id,
                                    tool=getattr(tool, "name", "unknown"), reads=record["reads"],
                                    questions=record["questions"],
                                    unknown_answers=record.get("unknown_answers", 0))
            self.runtime.trace.emit("direct_supervision_finished", tool_call_id=call_id,
                                    reads=record["reads"], questions=record["questions"])
            if self.current is record:
                self.current = None

    async def _monitor(self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
                       wake: asyncio.Event, worker: asyncio.Task[Any]) -> None:
        last_progress_wall = time.monotonic()
        last_review = 0.0
        last_marker: tuple[Any, Any] | None = None
        last_phase: Any = None
        while not worker.done() and not record["cancel_requested"]:
            try:
                await asyncio.wait_for(wake.wait(), timeout=0.25)
                wake.clear()
            except asyncio.TimeoutError:
                pass
            execution = self._snapshot(record["call_id"])
            if not execution:
                continue
            if execution.get("state") in {"succeeded", "failed", "cancelled", "unknown"}:
                return
            marker = (execution.get("last_progress_at"), execution.get("progress_signal"))
            if marker[0] is not None and marker != last_marker:
                last_progress_wall = time.monotonic()
                self.runtime.trace.emit("call_supervision_progress", tool_call_id=record["call_id"],
                                        tool=getattr(tool, "name", "unknown"), execution=execution)
            last_marker = marker
            phase = execution.get("phase")
            if last_phase is not None and phase != last_phase:
                last_progress_wall = time.monotonic()
            last_phase = phase
            now = time.monotonic()
            idle_s = now - last_progress_wall
            if idle_s < self.suspicion_s or now - last_review < self.review_interval_s:
                continue
            if record["questions"] >= self.max_questions:
                self.runtime.trace.emit("call_supervision_limit", tool_call_id=record["call_id"],
                                        tool=getattr(tool, "name", "unknown"), kind="questions",
                                        limit=self.max_questions)
                return
            last_review = now
            read: dict[str, Any] = {}
            if record["reads"] < self.max_reads:
                read = await self._bounded_read(record, tool, params)
                if worker.done() or record["cancel_requested"]:
                    return
                blocker = self._deterministic_terminal(execution, read)
                if blocker is not None:
                    await self._handoff(record, execution, blocker, decision=None)
                    return
            choice = await self.judge(record["call_id"], tool, params,
                                      self._snapshot(record["call_id"]), idle_s)
            record["questions"] += 1
            if choice == "unknown":
                record["unknown_answers"] = record.get("unknown_answers", 0) + 1
            if worker.done() or record["cancel_requested"]:
                return
            if choice == "yield":
                await self._handoff(record, self._snapshot(record["call_id"]),
                                    {"reason": "supervision_yield", "confirmed": False},
                                    decision=choice)
                return
            if choice == "inspect" and record["reads"] >= self.max_reads:
                self.runtime.trace.emit("call_supervision_limit", tool_call_id=record["call_id"],
                                        tool=getattr(tool, "name", "unknown"), kind="reads",
                                        limit=self.max_reads)

    async def _handoff(self, record: dict[str, Any], execution: Mapping[str, Any],
                       details: Mapping[str, Any], *, decision: str | None) -> None:
        if self.current is not record or record["cancel_requested"]:
            return
        record["cancel_requested"] = record["settling"] = True
        call_id = record["call_id"]
        safe = self._handoff_facts(self._snapshot(call_id) or execution,
                                   reason=str(details.get("reason") or "supervision_yield"),
                                   confirmed=bool(details.get("confirmed")), decision=decision,
                                   extra=details)
        self.runtime.trace.emit("direct_call_blocked", tool_call_id=call_id, facts=safe)
        self.runtime.trace.emit("call_supervision_stop_requested", tool_call_id=call_id,
                                tool=execution.get("tool"), reason=safe["reason"], decision=decision)
        kernel = self.runtime.responses_kernel
        if kernel is not None:
            kernel.notify_fact({"call_id": call_id, "status": "handoff_requested", **safe})
        started = time.monotonic()
        settled = await self.runtime.cancel_active_execution_with_facts("direct_call:" + safe["reason"])
        self.runtime.trace.emit("call_supervision_stop_settled", tool_call_id=call_id,
                                elapsed_s=time.monotonic() - started,
                                settled=bool(settled.get("settled")) if isinstance(settled, Mapping) else None)
        self.runtime.trace.emit("direct_call_handoff", tool_call_id=call_id, facts=safe, settlement=settled)
        self.runtime.direct_call_handoffs[call_id] = {"facts": safe, "settlement": settled}


# Compatibility name for slice-1 callers; the implementation now covers all
# mutating calls.
DirectBuildSupervisor = CallSupervisor

__all__ = ["CallSupervisor", "DirectBuildSupervisor", "MAX_SUPERVISION_READS", "MAX_SUPERVISION_QUESTIONS"]
