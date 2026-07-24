"""Transport-independent Body client backed by the Scarpet app."""

from __future__ import annotations

import time
import re
from typing import Any, NoReturn

from minebot.contract import (
    Action,
    BodyState,
    Event,
    InventorySlot,
    PerceptionResult,
    Result,
    execution_checkpoint,
    perception_next_cursor,
)
from minebot.game.action_reconciliation import (
    ActionReconciliationStatus,
    block_probe_position,
    classify_authoritative_block,
    is_mutating_action,
    reconciled_terminal_event,
    terminal_event_name,
)
from minebot.game.errors import ActionReconciliationUnknownError, BodyActionTimeoutError, BodyProtocolError
from minebot.game.protocol import (
    RCON_SLOT_PAGE_SIZE,
    build_action_call,
    build_action_status_call,
    build_chat_drain_call,
    build_despawn_call,
    build_drain_call,
    build_event_head_call,
    build_interrupt_call,
    build_perceive_call,
    build_say_call,
    build_spawn_call,
    build_state_call,
    parse_events,
    parse_events_page,
    parse_perception,
    parse_result,
    parse_state,
)
from minebot.game.transport import BodyTransport

DEFAULT_TERMINAL_EVENTS = {
    "moveDone",
    "navigateDone",
    "lookDone",
    "jumpDone",
    "selectSlotDone",
    "selectItemDone",
    "stopDone",
    "mineDone",
    "placeDone",
    "useDone",
    "rangedDone",
    "igniteDone",
    "sowDone",
    "attackDone",
    "engageDone",
    "craftDone",
    "containerDone",
    "dropDone",
    "handoffDone",
    "furnaceDone",
    "moveItemDone",
    "death",
    "respawned",
    "interrupted",
}
GLOBAL_TERMINAL_EVENTS = {
    "death",
    "bodyMissing",
    "respawned",
    "ownerPreempted",
    "interrupted",
}
RESULT_TERMINAL_ACTIONS = {"navigationMutationDecision"}
ACTION_RECONCILIATION_POLL_S = 0.50
ACTION_RECONCILIATION_MAX_WAIT_S = 60.0
# A server can release the action owner one tick before its terminal event is
# visible to the event drain.  Keep a short read-only settle window so that
# race is not reported as an outcome-unknown dispatch.
ACTION_RECONCILIATION_SETTLE_GRACE_S = 3.0
MAX_MINECRAFT_USERNAME_LENGTH = 16
CHAT_TEXT_LIMIT = 220
_MINECRAFT_FORMATTING_RE = re.compile(r"§.")
_NUMBER_PATTERN = r"-?\d+(?:\.\d+)?"
_COORDINATE_TRIPLE_RE = re.compile(
    rf"(?<![\w.-])[\[(]\s*{_NUMBER_PATTERN}\s*[,，]\s*{_NUMBER_PATTERN}\s*[,，]\s*{_NUMBER_PATTERN}\s*[\])]"
)
_BARE_COORDINATE_TRIPLE_RE = re.compile(
    rf"(?<![\w.-]){_NUMBER_PATTERN}\s*[,，]\s*{_NUMBER_PATTERN}\s*[,，]\s*{_NUMBER_PATTERN}(?![\w-])"
)
_AXIS_COORDINATE_TRIPLE_RE = re.compile(
    rf"(?i)(?<!\w)x\s*[:=]?\s*{_NUMBER_PATTERN}\s*[,，;；]?\s*"
    rf"y\s*[:=]?\s*{_NUMBER_PATTERN}\s*[,，;；]?\s*"
    rf"z\s*[:=]?\s*{_NUMBER_PATTERN}(?!\w)"
)
_LABELED_SPACE_COORDINATE_TRIPLE_RE = re.compile(
    rf"(?i)(?<!\w)(?:coordinates?|coords?|position|pos|located\s+at|at|坐标|位置|位于)"
    rf"\s*(?:is|are|about|around|roughly|approximately|为|是|在|约|大约|大概)?\s*[:：=]?\s*"
    rf"{_NUMBER_PATTERN}\s+{_NUMBER_PATTERN}\s+{_NUMBER_PATTERN}(?![\w-])"
)


class ScarpetBody:
    def __init__(self, bot_name: str, transport: BodyTransport, app: str = "minebot"):
        self.bot_name = bot_name
        self.transport = transport
        self.app = app
        self.last_seq = 0
        self.last_chat_seq = 0
        self.event_log: list[Event] = []
        self.request_history: list[dict[str, object]] = []
        self.completed_action_traces: list[dict[str, object]] = []
        self._inflight_action_traces: dict[str, dict[str, object]] = {}
        self._action_start_seqs: dict[str, int] = {}
        self._await_consumed_event_seqs: set[int] = set()
        # Same-ID replay is only safe inside the server's current receive
        # ledger epoch. The event-head handshake establishes this value;
        # an app reload/reset makes an old dispatch ambiguous.
        self._server_epoch: str | None = None

    def _record_request(
        self,
        *,
        kind: str,
        started_at: float,
        elapsed_ms: float,
        command: str,
        action_id: str | None = None,
        action_name: str | None = None,
        scope: str | None = None,
        ok: bool = True,
        error_type: str | None = None,
        error: str | None = None,
    ) -> None:
        entry: dict[str, object] = {
            "kind": kind,
            "started_at": round(started_at, 6),
            "elapsed_ms": round(elapsed_ms, 3),
            "ok": ok,
            "command_len": len(command),
        }
        if ok:
            entry["command"] = command
        else:
            entry["command_head"] = command[:160]
            if error_type is not None:
                entry["error_type"] = error_type
            if error is not None:
                entry["error"] = error
        if action_id is not None:
            entry["action_id"] = action_id
        if action_name is not None:
            entry["action_name"] = action_name
        if scope is not None:
            entry["scope"] = scope
        self.request_history.append(entry)

    def _timed_request(
        self,
        command: str,
        *,
        kind: str,
        action_id: str | None = None,
        action_name: str | None = None,
        scope: str | None = None,
        retry: bool = True,
    ) -> str:
        execution_checkpoint()
        started = time.monotonic()
        try:
            raw = self._transport_request(command, retry=retry)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self._record_request(
                kind=kind,
                started_at=time.time(),
                elapsed_ms=elapsed_ms,
                command=command,
                action_id=action_id,
                action_name=action_name,
                scope=scope,
                ok=False,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        elapsed_ms = (time.monotonic() - started) * 1000.0
        self._record_request(
            kind=kind,
            started_at=time.time(),
            elapsed_ms=elapsed_ms,
            command=command,
            action_id=action_id,
            action_name=action_name,
            scope=scope,
        )
        execution_checkpoint()
        return raw

    def _transport_request(self, command: str, *, retry: bool) -> str:
        if retry:
            return self.transport.request(command)
        request_once = getattr(self.transport, "request_once", None)
        if callable(request_once):
            return request_once(command)
        # Test doubles and transports without a retry policy are already
        # single-shot; preserving their request boundary keeps the contract
        # transport-agnostic.
        return self.transport.request(command)

    def _reconnect_transport(self) -> None:
        reconnect = getattr(self.transport, "reconnect", None)
        if callable(reconnect):
            reconnect()

    def _begin_action_trace(
        self,
        action: Action,
        action_start_seq: int,
        result: Result | None,
        *,
        dispatch_error: str | None = None,
        dispatch_epoch: str | None = None,
    ) -> None:
        self._inflight_action_traces[action.id] = {
            "action_id": action.id,
            "action_name": action.name,
            "params": dict(action.params),
            "dispatch_ok": bool(result.ok) if result is not None else False,
            "accepted": bool(result.accepted) if result is not None else False,
            "dispatch_error": dispatch_error if dispatch_error is not None else (result.error if result else None),
            "dispatch_result": dict(result.data) if result is not None else {},
            "dispatch_epoch": dispatch_epoch,
        }
        self._action_start_seqs[action.id] = action_start_seq

    def _finish_action_without_terminal(self, action_id: str) -> None:
        trace = dict(self._inflight_action_traces.pop(action_id, {}))
        self._action_start_seqs.pop(action_id, None)
        trace.update(
            {
                "terminal_event": None,
                "terminal_seq": None,
                "terminal_tick": None,
                "terminal_data": None,
                "wait_ms": 0.0,
                "poll_count": 0,
                "observed_events": 0,
            }
        )
        self._append_completed_action_trace(trace)

    def _status_terminal_event(self, status: Result) -> Event | None:
        raw = status.data.get("terminal")
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        data = raw.get("data")
        if not isinstance(name, str) or not isinstance(data, dict):
            return None
        try:
            seq = int(raw.get("seq") or 0)
            tick = int(raw.get("tick") or 0)
        except (TypeError, ValueError):
            return None
        return Event(
            seq=seq,
            tick=tick,
            bot=str(raw.get("bot") or self.bot_name),
            name=name,
            data=dict(data),
        )

    def _status_dispatch_result(self, status: Result) -> Result | None:
        raw = status.data.get("dispatch")
        if not isinstance(raw, dict):
            return None
        try:
            return Result(
                id=raw.get("id"),
                bot=str(raw.get("bot") or self.bot_name),
                type="result",
                ok=bool(raw.get("ok")),
                accepted=bool(raw.get("accepted")),
                complete=bool(raw.get("complete", True)),
                data=dict(raw.get("data") or {}),
                error=raw.get("error"),
            )
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _status_proves_not_seen(status: Result, expected_epoch: str | None) -> bool:
        """Return true only for an explicit, bounded server receive verdict.

        A missing dispatch cache is otherwise ambiguous: the action may have
        been accepted and its cache may have been evicted.  The Scarpet status
        endpoint emits ``not_seen`` only while its bounded receive ledger is
        complete, so this predicate is the sole path that permits a same-ID
        resend without an authoritative world probe.
        """

        return (
            expected_epoch is not None
            and status.data.get("epoch") == expected_epoch
            and status.data.get("dispatch_state") == "not_seen"
            and status.data.get("seen") is False
            and status.data.get("retention_complete") is True
        )

    def _matching_terminal_event(self, action: Action) -> Event | None:
        expected = terminal_event_name(action.name)
        if expected is None:
            return None
        for event in reversed(self.event_log):
            if event.name == expected and event.data.get("action_id") == action.id:
                return event
        try:
            for event in self.poll_events():
                if event.name == expected and event.data.get("action_id") == action.id:
                    return event
        except Exception:
            return None
        return None

    def _register_reconciled_terminal(self, action: Action, event: Event) -> Event:
        for existing in reversed(self.event_log):
            if existing.name == event.name and existing.data.get("action_id") == action.id:
                return existing
        seq = int(event.seq)
        after_seq = self._action_start_seqs.get(action.id, self.last_seq)
        if seq <= max(self.last_seq, after_seq):
            seq = max(self.last_seq, after_seq) + 1
        normalized = Event(
            seq=seq,
            tick=event.tick,
            bot=self.bot_name,
            name=event.name,
            data=dict(event.data),
        )
        self.event_log.append(normalized)
        self.last_seq = max(self.last_seq, normalized.seq)
        return normalized

    def _raise_reconciliation_unknown(
        self,
        action: Action,
        *,
        evidence: dict[str, object],
        cause: BaseException | None = None,
    ) -> NoReturn:
        trace = self._inflight_action_traces.pop(action.id, {})
        self._action_start_seqs.pop(action.id, None)
        trace = dict(trace)
        trace["reconciliation_status"] = ActionReconciliationStatus.UNKNOWN.value
        trace["reconciliation_evidence"] = dict(evidence)
        if cause is not None:
            trace["reconciliation_error"] = {
                "type": type(cause).__name__,
                "message": str(cause),
            }
        trace.update(
            {
                "terminal_event": None,
                "terminal_seq": None,
                "terminal_tick": None,
                "terminal_data": None,
                "wait_ms": 0.0,
                "poll_count": 0,
                "observed_events": 0,
            }
        )
        self._append_completed_action_trace(trace)
        diagnostics = {
            "action_id": action.id,
            "action_name": action.name,
            "status": ActionReconciliationStatus.UNKNOWN.value,
            "evidence": dict(evidence),
        }
        if cause is not None:
            diagnostics["cause_type"] = type(cause).__name__
            diagnostics["cause"] = str(cause)
        raise ActionReconciliationUnknownError(
            f"action outcome unknown after transport failure: {action.name} ({action.id})",
            diagnostics=diagnostics,
        ) from cause

    def _reconcile_mutation_dispatch(
        self,
        action: Action,
        action_start_seq: int,
        dispatch_error: BaseException,
        dispatch_epoch: str | None,
    ) -> Result:
        self._begin_action_trace(
            action,
            action_start_seq,
            None,
            dispatch_error=f"{type(dispatch_error).__name__}: {dispatch_error}",
            dispatch_epoch=dispatch_epoch,
        )
        evidence: dict[str, object] = {
            "dispatch_failure": {
                "type": type(dispatch_error).__name__,
                "message": str(dispatch_error),
            },
            "status_lookup": "pending",
        }
        try:
            self._reconnect_transport()
            status = parse_result(
                self._timed_request(
                    build_action_status_call(self.bot_name, action.id, self.app),
                    kind="action_reconcile_status",
                    action_id=action.id,
                    action_name=action.name,
                    retry=True,
                )
            )
        except Exception as exc:
            self._raise_reconciliation_unknown(action, evidence=evidence, cause=exc)

        evidence["status_lookup"] = "ok" if status.ok else "rejected"
        status_epoch = status.data.get("epoch")
        evidence["dispatch_epoch"] = dispatch_epoch
        evidence["status_epoch"] = status_epoch
        evidence["epoch_match"] = (
            dispatch_epoch is not None
            and isinstance(status_epoch, str)
            and status_epoch == dispatch_epoch
        )
        self._inflight_action_traces[action.id]["reconciliation_evidence"] = dict(evidence)
        remembered = self._status_dispatch_result(status)
        if remembered is not None:
            self._inflight_action_traces[action.id]["dispatch_result"] = dict(remembered.data)
            self._inflight_action_traces[action.id]["dispatch_ok"] = remembered.ok
            self._inflight_action_traces[action.id]["accepted"] = remembered.accepted
            self._inflight_action_traces[action.id]["dispatch_error"] = remembered.error
            if not (remembered.ok and remembered.accepted):
                self._inflight_action_traces[action.id]["reconciliation_status"] = "not_applied"
                self._finish_action_without_terminal(action.id)
                return remembered

            # A result-terminal action has no follow-up event.  The dispatch
            # cache is its authoritative terminal fact, so an accepted cached
            # result is already a settled action.
            if action.name in RESULT_TERMINAL_ACTIONS:
                self._inflight_action_traces[action.id]["reconciliation_status"] = "applied"
                self._finish_action_without_terminal(action.id)
                return remembered

        if self._status_proves_not_seen(status, dispatch_epoch):
            evidence["dispatch_state"] = "not_seen"
            self._inflight_action_traces[action.id]["reconciliation_status"] = "not_applied"
            self._inflight_action_traces[action.id]["reconciliation_evidence"] = dict(evidence)
            try:
                resent = parse_result(
                    self._timed_request(
                        build_action_call(self.bot_name, action, self.app),
                        kind="action_dispatch_reconcile_resend_not_seen",
                        action_id=action.id,
                        action_name=action.name,
                        retry=False,
                    )
                )
            except Exception as exc:
                self._raise_reconciliation_unknown(action, evidence=evidence, cause=exc)
            self._inflight_action_traces[action.id]["dispatch_result"] = dict(resent.data)
            self._inflight_action_traces[action.id]["dispatch_ok"] = resent.ok
            self._inflight_action_traces[action.id]["accepted"] = resent.accepted
            self._inflight_action_traces[action.id]["dispatch_error"] = resent.error
            if not (resent.ok and resent.accepted):
                self._finish_action_without_terminal(action.id)
            return resent

        terminal = self._wait_for_reconciled_terminal(action, status, evidence)
        if terminal is not None:
            evidence["terminal_event"] = terminal.name
            # A matching terminal, including an honest failure terminal, is a
            # settled dispatch.  It must not be replayed merely because its
            # outcome was unsuccessful.
            self._inflight_action_traces[action.id]["reconciliation_status"] = "applied"
            self._inflight_action_traces[action.id]["reconciliation_evidence"] = dict(evidence)
            self._register_reconciled_terminal(action, terminal)
            return Result(
                id=action.id,
                bot=self.bot_name,
                type="result",
                ok=True,
                accepted=True,
                complete=True,
                data={"action": action.name, "reconciled": "terminal_event"},
                error=None,
            )

        target = block_probe_position(action)
        if target is not None:
            try:
                perception = self.perceive(
                    "blockAt",
                    {"x": target[0], "y": target[1], "z": target[2]},
                )
                if not perception.ok:
                    raise BodyProtocolError(perception.error or "block probe rejected")
                current_type = str(perception.data.get("type") or "unknown")
                classification = classify_authoritative_block(action, current_type)
            except Exception as exc:
                self._raise_reconciliation_unknown(action, evidence=evidence, cause=exc)
            evidence["block"] = dict(classification.evidence)
            self._inflight_action_traces[action.id]["reconciliation_evidence"] = dict(evidence)
            if classification.status is ActionReconciliationStatus.APPLIED:
                self._inflight_action_traces[action.id]["reconciliation_status"] = "applied"
                synthetic = reconciled_terminal_event(action, classification)
                if synthetic is not None:
                    self._register_reconciled_terminal(action, synthetic)
                    return Result(
                        id=action.id,
                        bot=self.bot_name,
                        type="result",
                        ok=True,
                        accepted=True,
                        complete=True,
                        data={"action": action.name, "reconciled": "authoritative_world"},
                        error=None,
                    )
            if classification.status is ActionReconciliationStatus.NOT_APPLIED:
                # A block fact can prove the target is currently unchanged,
                # but it cannot prove an old dispatch from a different server
                # epoch was cancelled.  Preserve exactly-once across reloads:
                # only resend when the receive ledger epoch also matches.
                if evidence.get("epoch_match") is not True:
                    self._raise_reconciliation_unknown(action, evidence=evidence)
                try:
                    resent = parse_result(
                        self._timed_request(
                            build_action_call(self.bot_name, action, self.app),
                            kind="action_dispatch_reconcile_resend",
                            action_id=action.id,
                            action_name=action.name,
                            retry=False,
                        )
                    )
                except Exception as exc:
                    self._raise_reconciliation_unknown(action, evidence=evidence, cause=exc)
                self._inflight_action_traces[action.id]["reconciliation_status"] = "not_applied"
                self._inflight_action_traces[action.id]["reconciliation_evidence"] = dict(evidence)
                self._inflight_action_traces[action.id]["dispatch_result"] = dict(resent.data)
                self._inflight_action_traces[action.id]["dispatch_ok"] = resent.ok
                self._inflight_action_traces[action.id]["accepted"] = resent.accepted
                self._inflight_action_traces[action.id]["dispatch_error"] = resent.error
                if not (resent.ok and resent.accepted):
                    self._finish_action_without_terminal(action.id)
                return resent

        self._raise_reconciliation_unknown(action, evidence=evidence)

    def _wait_for_reconciled_terminal(
        self,
        action: Action,
        status: Result,
        evidence: dict[str, object],
    ) -> Event | None:
        """Observe an in-flight action without issuing another mutation.

        A lost dispatch response is not evidence that a composite Body action
        stopped.  The action-id cache, event stream, and Body activity state
        are read-only sources.  While the Body still reports an owner or a
        pending action, keep observing until the matching terminal appears.
        If the action is no longer active and no terminal is authoritative,
        the caller remains fail-closed and returns ``unknown``.
        """

        deadline = time.monotonic() + self._action_reconciliation_wait_s(action)
        inactive_since: float | None = None
        current_status = status
        while True:
            terminal = self._status_terminal_event(current_status)
            if terminal is None:
                terminal = self._matching_terminal_event(action)
            if terminal is not None:
                return terminal

            active, state_evidence = self._action_activity_snapshot(action.id)
            evidence["active_state"] = state_evidence
            if active is False:
                # Owner cleanup and event publication are separate server-side
                # steps.  A terminal can therefore become visible just after
                # the first drain observes owner=null.  Keep observing for a
                # short, bounded, read-only grace period instead of turning
                # that ordering race into a false unknown.
                if inactive_since is None:
                    inactive_since = time.monotonic()
                    evidence["inactive_settle_started"] = True
                event_seq = state_evidence.get("event_seq")
                try:
                    event_seq_value = int(event_seq) if event_seq is not None else 0
                except (TypeError, ValueError):
                    event_seq_value = 0
                if event_seq_value > self.last_seq:
                    late_terminal = self._matching_terminal_event(action)
                    if late_terminal is not None:
                        return late_terminal
                if time.monotonic() - inactive_since >= ACTION_RECONCILIATION_SETTLE_GRACE_S:
                    evidence["inactive_settle_timeout"] = True
                    return None
            elif active is None:
                # A failed authoritative read cannot distinguish an active
                # action from a stopped one.  Preserve the fail-closed
                # unknown instead of inventing liveness or replaying.
                return None
            if time.monotonic() >= deadline:
                evidence["active_state_timeout"] = True
                return None

            time.sleep(ACTION_RECONCILIATION_POLL_S)
            try:
                current_status = parse_result(
                    self._timed_request(
                        build_action_status_call(self.bot_name, action.id, self.app),
                        kind="action_reconcile_status_poll",
                        action_id=action.id,
                        action_name=action.name,
                        retry=True,
                    )
                )
            except Exception as exc:
                errors = evidence.setdefault("status_poll_errors", [])
                if isinstance(errors, list):
                    errors.append({"type": type(exc).__name__, "message": str(exc)})
                # A failed read cannot establish that the action stopped. Keep
                # the wait bounded, then yield typed unknown.
                if time.monotonic() >= deadline:
                    return None

    def _action_activity_snapshot(self, action_id: str) -> tuple[bool | None, dict[str, object]]:
        try:
            head = self.event_head(f"action-reconcile-{action_id}")
        except Exception as head_exc:
            head = None
            head_error = f"{type(head_exc).__name__}: {head_exc}"
        else:
            head_error = None
        if head is not None:
            owner = head.get("owner")
            pending = head.get("pending_action_count")
            active = owner is not None or (pending is not None and int(pending) > 0)
            evidence: dict[str, object] = {
                "source": "event_head",
                "event_seq": head.get("event_seq"),
                "body_owner": owner,
                "pending_action_count": pending,
            }
            if head_error is not None:
                evidence["fallback_error"] = head_error
            return active, evidence
        try:
            state = self.get_state()
        except Exception as exc:
            return None, {
                "source": "state",
                "read_error": f"{type(exc).__name__}: {exc}",
                "event_head_error": head_error,
            }
        pending = state.pending_action_count
        active = state.body_owner is not None or (pending is not None and pending > 0)
        return active, {
            "source": "state",
            "event_seq": None,
            "body_owner": state.body_owner,
            "pending_action_count": pending,
            "health": state.health,
            "missing": state.missing,
            "event_head_error": head_error,
        }

    @staticmethod
    def _action_reconciliation_wait_s(action: Action) -> float:
        raw_ticks = action.params.get("timeout_ticks")
        try:
            timeout_ticks = float(raw_ticks)
        except (TypeError, ValueError):
            timeout_ticks = 0.0
        if timeout_ticks > 0:
            return min(ACTION_RECONCILIATION_MAX_WAIT_S, max(5.0, timeout_ticks / 20.0 + 5.0))
        return 15.0

    def _append_completed_action_trace(self, trace: dict[str, object]) -> None:
        self.completed_action_traces.append(trace)

    def _finish_action_trace(
        self,
        action_id: str,
        *,
        terminal: Event,
        wait_ms: float,
        poll_count: int,
        observed_events: int,
    ) -> None:
        trace = dict(self._inflight_action_traces.pop(action_id, {}))
        self._action_start_seqs.pop(action_id, None)
        trace["terminal_event"] = terminal.name
        trace["terminal_seq"] = terminal.seq
        trace["terminal_tick"] = terminal.tick
        trace["terminal_data"] = dict(terminal.data)
        trace["wait_ms"] = round(wait_ms, 3)
        trace["poll_count"] = poll_count
        trace["observed_events"] = observed_events
        self._append_completed_action_trace(trace)

    def _record_action_intermediate(self, action_id: str, event: Event) -> None:
        trace = self._inflight_action_traces.get(action_id)
        if trace is None:
            return
        events = trace.setdefault("intermediate_events", [])
        if isinstance(events, list):
            events.append(
                {
                    "seq": event.seq,
                    "tick": event.tick,
                    "name": event.name,
                    "data": dict(event.data),
                }
            )

    def transport_latency_snapshot(self, *, max_requests: int = 64) -> dict[str, object]:
        requests = self.request_history[-max_requests:] if max_requests > 0 else self.request_history
        latencies = [float(entry["elapsed_ms"]) for entry in requests]
        extra = _transport_snapshot(self.transport)
        if not latencies:
            base = {
                "count": 0,
                "last_request_ms": None,
                "mean_request_ms": None,
                "p95_request_ms": None,
                "max_request_ms": None,
            }
            base.update(extra)
            return base
        ordered = sorted(latencies)
        p95_index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * 0.95)))
        base = {
            "count": len(latencies),
            "last_request_ms": latencies[-1],
            "mean_request_ms": round(sum(latencies) / len(latencies), 3),
            "p95_request_ms": ordered[p95_index],
            "max_request_ms": max(latencies),
        }
        base.update(extra)
        return base

    def observability_snapshot(
        self,
        *,
        max_events: int = 64,
        max_traces: int = 32,
        max_requests: int = 64,
    ) -> dict[str, object]:
        events = self.event_log[-max_events:] if max_events > 0 else self.event_log
        traces = self.completed_action_traces[-max_traces:] if max_traces > 0 else self.completed_action_traces
        requests = self.request_history[-max_requests:] if max_requests > 0 else self.request_history
        return {
            "bot": self.bot_name,
            "server_epoch": self._server_epoch,
            "events": [
                {
                    "seq": event.seq,
                    "tick": event.tick,
                    "bot": event.bot,
                    "name": event.name,
                    "data": dict(event.data),
                }
                for event in events
            ],
            "action_traces": [dict(trace) for trace in traces],
            "request_history": [dict(entry) for entry in requests],
            "transport": self.transport_latency_snapshot(max_requests=max_requests),
        }

    def spawn(
        self,
        pos: tuple[int, int, int] | None = None,
        timeout_s: float = 15.0,
        *,
        yaw: float | None = None,
        pitch: float | None = None,
        dimension: str | None = None,
        gamemode: str | None = None,
        emit_respawned: bool = False,
    ) -> Result:
        if len(self.bot_name) > MAX_MINECRAFT_USERNAME_LENGTH:
            return Result(
                id=None,
                bot=self.bot_name,
                type="result",
                ok=False,
                accepted=False,
                complete=True,
                data={
                    "action": "spawn",
                    "reason": "bot_name_too_long",
                    "max_length": MAX_MINECRAFT_USERNAME_LENGTH,
                    "actual_length": len(self.bot_name),
                },
                error="invalid_bot_name",
            )

        result = parse_result(
            self._timed_request(
                build_spawn_call(
                    self.bot_name,
                    pos,
                    self.app,
                    yaw=yaw,
                    pitch=pitch,
                    dimension=dimension,
                    gamemode=gamemode,
                    emit_respawned=emit_respawned,
                ),
                kind="spawn",
            )
        )
        if not (result.ok and result.accepted):
            return result

        deadline = time.time() + timeout_s
        last_error: str | None = None
        while time.time() < deadline:
            try:
                state = self.get_state()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.1)
                continue
            if not state.missing:
                return result
            last_error = "spawn_pending"
            time.sleep(0.1)

        return Result(
            id=result.id,
            bot=result.bot,
            type=result.type,
            ok=False,
            accepted=result.accepted,
            complete=result.complete,
            data=dict(result.data),
            error=last_error or "spawn_timeout",
        )

    def despawn(self) -> Result:
        return parse_result(self._timed_request(build_despawn_call(self.bot_name, self.app), kind="despawn"))

    def say(self, text: str) -> bool:
        cleaned = _sanitize_chat_text(text)
        if not cleaned:
            return True
        try:
            result = parse_result(
                self._timed_request(
                    build_say_call(self.bot_name, cleaned, self.app),
                    kind="say",
                )
            )
        except Exception:
            return False
        return bool(result.ok and result.accepted and result.data.get("said") is True)

    def get_state(self) -> BodyState:
        return parse_state(self._timed_request(build_state_call(self.bot_name, self.app), kind="state"))

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        return parse_perception(
            self._timed_request(
                build_perceive_call(self.bot_name, scope, params, self.app),
                kind="perception",
                scope=scope,
            )
        )

    def get_inventory(self, page_size: int = RCON_SLOT_PAGE_SIZE) -> list[InventorySlot]:
        page_size = max(1, min(int(page_size), RCON_SLOT_PAGE_SIZE))
        slots: list[InventorySlot] = []
        start: int | None = 0
        while start is not None:
            perception = self.perceive("inventory", {"start": start, "limit": page_size})
            if not perception.ok:
                raise ValueError(f"inventory perception failed: {perception.error}")
            slots.extend(InventorySlot.from_payload(item) for item in perception.data.get("slots") or [])
            start = _next_start(perception)
            if start is not None:
                start = int(start)
        return slots

    def get_container(
        self,
        pos: tuple[int, int, int],
        *,
        total_slots: int = 27,
        page_size: int = RCON_SLOT_PAGE_SIZE,
    ) -> list[InventorySlot]:
        page_size = max(1, min(int(page_size), RCON_SLOT_PAGE_SIZE))
        slots: list[InventorySlot] = []
        start: int | None = 0
        while start is not None:
            perception = self.perceive(
                "container",
                {"pos": list(pos), "start": start, "limit": page_size, "total_slots": total_slots},
            )
            if not perception.ok:
                raise ValueError(f"container perception failed: {perception.error}")
            slots.extend(InventorySlot.from_payload(item) for item in perception.data.get("slots") or [])
            start = _next_start(perception)
            if start is not None:
                start = int(start)
        return slots

    def execute(self, action: Action) -> Result:
        action_start_seq = self.last_seq
        dispatch_epoch = self._server_epoch
        try:
            result = parse_result(
                self._timed_request(
                    build_action_call(self.bot_name, action, self.app),
                    kind="action_dispatch",
                    action_id=action.id,
                    action_name=action.name,
                    retry=not is_mutating_action(action.name),
                )
            )
        except (OSError, BodyProtocolError) as exc:
            if is_mutating_action(action.name):
                return self._reconcile_mutation_dispatch(action, action_start_seq, exc, dispatch_epoch)
            raise
        self._begin_action_trace(action, action_start_seq, result, dispatch_epoch=dispatch_epoch)
        if not (result.ok and result.accepted):
            self._finish_action_without_terminal(action.id)
        elif action.name in RESULT_TERMINAL_ACTIONS:
            trace = dict(self._inflight_action_traces.pop(action.id))
            trace.update(
                {
                    "terminal_event": "actionResult",
                    "terminal_seq": None,
                    "terminal_tick": None,
                    "terminal_data": dict(result.data),
                    "wait_ms": 0.0,
                    "poll_count": 0,
                    "observed_events": 0,
                }
            )
            self._append_completed_action_trace(trace)
            self._action_start_seqs.pop(action.id, None)
        return result

    def _dispatch_action_and_await(
        self,
        action: Action,
        *,
        timeout_s: float,
        action_name: str,
    ) -> Event:
        result = self.execute(action)
        if result.error is not None:
            raise ValueError(f"{action_name} rejected: {result.error}")
        return self.await_action_terminal(action.id, timeout_s=timeout_s)

    def jump(self, timeout_s: float = 2.0) -> Event:
        action = Action.create("jump", {})
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="jump")

    def select_item(self, item: str, timeout_s: float = 2.0) -> Event:
        action = Action.create("selectItem", {"item": item})
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="selectItem")

    def use_item(
        self,
        *,
        mode: str = "once",
        ticks: int = 1,
        item: str | None = None,
        slot: int | None = None,
        timeout_s: float = 5.0,
    ) -> Event:
        params: dict[str, object] = {"mode": mode, "ticks": ticks}
        if item is not None:
            params["item"] = item
        if slot is not None:
            params["slot"] = slot
        action = Action.create("useItem", params)
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="useItem")

    def ignite_block(
        self,
        pos: tuple[int, int, int],
        *,
        item: str | None = None,
        allow_server_substitute: bool = False,
        timeout_s: float = 8.0,
    ) -> Event:
        """Server-side fire primitive: physical `player use` first, then an
        optional server substitute when physical use is accepted but no fire is
        authoritatively observable. The caller must equip/select the ignition
        item."""
        params: dict[str, object] = {"target": list(pos)}
        if item is not None:
            params["item"] = item
        if allow_server_substitute:
            params["allow_server_substitute"] = True
        action = Action.create("igniteBlock", params)
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="igniteBlock")

    def sow_crop(
        self,
        pos: tuple[int, int, int],
        *,
        crop_block: str,
        seed_item: str | None = None,
        allow_server_substitute: bool = False,
        timeout_s: float = 8.0,
    ) -> Event:
        """Server-side sow primitive: physical `player use` first, then an
        optional server substitute when the use is accepted but no crop becomes
        authoritatively observable. The caller must equip/select the seed
        item."""
        params: dict[str, object] = {"target": list(pos), "crop_block": crop_block}
        if seed_item is not None:
            params["seed_item"] = seed_item
        if allow_server_substitute:
            params["allow_server_substitute"] = True
        action = Action.create("sowCrop", params)
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="sowCrop")

    def attack_entity(
        self,
        *,
        target_type: str = "",
        target_name: str | None = None,
        radius: int = 4,
        timeout_ticks: int = 100,
        cooldown_ticks: int = 10,
        timeout_s: float = 10.0,
    ) -> Event:
        params: dict[str, object] = {
            "target_type": target_type,
            "radius": radius,
            "timeout_ticks": timeout_ticks,
            "cooldown_ticks": cooldown_ticks,
        }
        if target_name is not None:
            params["target_name"] = target_name
        action = Action.create("attackEntity", params)
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="attackEntity")

    def ranged_attack(
        self,
        *,
        weapon: str = "bow",
        target_type: str = "",
        target_id: str | None = None,
        target_name: str | None = None,
        radius: int = 24,
        timeout_ticks: int = 80,
        use_interval_ticks: int | None = None,
        expected_shots: int = 1,
        timeout_s: float = 10.0,
    ) -> Event:
        params: dict[str, object] = {
            "weapon": weapon,
            "target_type": target_type,
            "radius": radius,
            "timeout_ticks": timeout_ticks,
            "expected_shots": expected_shots,
        }
        if target_id is not None:
            params["target_id"] = target_id
        if target_name is not None:
            params["target_name"] = target_name
        if use_interval_ticks is not None:
            params["use_interval_ticks"] = use_interval_ticks
        action = Action.create("rangedAttack", params)
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="rangedAttack")

    def container_transfer(
        self,
        *,
        pos: tuple[int, int, int],
        direction: str,
        container_slot: int = 0,
        bot_slot: int = 0,
        count: int | None = None,
        max_stack: int = 64,
        timeout_s: float = 2.0,
    ) -> Event:
        params: dict[str, object] = {
            "pos": list(pos),
            "direction": direction,
            "container_slot": container_slot,
            "bot_slot": bot_slot,
            "max_stack": max_stack,
        }
        if count is not None:
            params["count"] = count
        action = Action.create(
            "containerTransfer",
            params,
        )
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="containerTransfer")

    def drop_item(
        self,
        *,
        slot: int,
        mode: str = "one",
        timeout_s: float = 2.0,
    ) -> Event:
        action = Action.create("dropItem", {"slot": slot, "mode": mode})
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="dropItem")

    def move_item(
        self,
        *,
        from_slot: int,
        to_slot: int,
        count: int | None = None,
        max_stack: int = 64,
        timeout_s: float = 2.0,
    ) -> Event:
        params: dict[str, object] = {"from_slot": from_slot, "to_slot": to_slot, "max_stack": max_stack}
        if count is not None:
            params["count"] = count
        action = Action.create("moveItem", params)
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="moveItem")

    def craft_item(
        self,
        *,
        inputs: list[dict[str, object]],
        output: dict[str, object],
        max_stack: int = 64,
        timeout_s: float = 2.0,
    ) -> Event:
        action = Action.create(
            "craftItem",
            {
                "inputs": inputs,
                "output": output,
                "max_stack": max_stack,
            },
        )
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="craftItem")

    def furnace_transfer(
        self,
        *,
        pos: tuple[int, int, int],
        direction: str,
        furnace_slot: str = "output",
        bot_slot: int = 0,
        count: int | None = None,
        max_stack: int = 64,
        timeout_s: float = 2.0,
    ) -> Event:
        params: dict[str, object] = {
            "pos": list(pos),
            "direction": direction,
            "furnace_slot": furnace_slot,
            "bot_slot": bot_slot,
            "max_stack": max_stack,
        }
        if count is not None:
            params["count"] = count
        action = Action.create("furnaceTransfer", params)
        return self._dispatch_action_and_await(action, timeout_s=timeout_s, action_name="furnaceTransfer")

    def poll_events(self) -> list[Event]:
        events = self._poll_events_pages(kind="event_drain", since_seq=self.last_seq)
        normalized: list[Event] = []
        for event in events:
            if event.seq <= self.last_seq:
                continue
            if event.seq != self.last_seq + 1:
                normalized.append(
                    Event(
                        seq=max(self.last_seq + 1, event.seq),
                        tick=event.tick,
                        bot=self.bot_name,
                        name="desync",
                        data={"expected_seq": self.last_seq + 1, "observed_seq": event.seq},
                    )
                )
            normalized.append(event)
            self.last_seq = max(self.last_seq, event.seq)
        self.event_log.extend(normalized)
        return normalized

    def event_head(self, proposed_epoch: str) -> dict[str, object]:
        result = parse_result(
            self._timed_request(
                build_event_head_call(self.bot_name, proposed_epoch, self.app),
                kind="event_head",
            )
        )
        epoch = str(result.data.get("epoch") or "")
        if not epoch:
            raise ValueError("event head is missing epoch")
        self._server_epoch = epoch
        head = {
            "event_seq": int(result.data.get("eventSeq") or 0),
            "chat_seq": int(result.data.get("chatSeq") or 0),
            "tick": int(result.data.get("tick") or 0),
            "epoch": epoch,
            "owner": None if result.data.get("owner") is None else str(result.data["owner"]),
        }
        if result.data.get("pendingActionCount") is not None:
            head["pending_action_count"] = int(result.data["pendingActionCount"])
        return head

    def _poll_events_pages(self, *, kind: str, since_seq: int, chat: bool = False) -> list[Event]:
        events: list[Event] = []
        cursor: int | None = since_seq
        while cursor is not None:
            command = (
                build_chat_drain_call(self.bot_name, self.app, since_seq=cursor)
                if chat
                else build_drain_call(self.bot_name, self.app, since_seq=cursor)
            )
            page, next_cursor = parse_events_page(self._timed_request(command, kind=kind))
            events.extend(page)
            cursor = int(next_cursor) if next_cursor is not None else None
        return events

    def poll_chat_events(self) -> list[Event]:
        """Drain app-level public chat directed at this bot.

        Chat is intentionally separate from Body action events so it cannot
        satisfy `await_action_terminal(...)` by accident.
        """
        events = self._poll_events_pages(kind="chat_drain", since_seq=self.last_chat_seq, chat=True)
        fresh = [event for event in events if event.seq > self.last_chat_seq]
        for event in fresh:
            self.last_chat_seq = max(self.last_chat_seq, event.seq)
        self.event_log.extend(fresh)
        return fresh

    def await_action_terminal(
        self,
        action_id: str,
        timeout_s: float = 15.0,
        poll_interval_s: float = 0.10,
        terminal_events: set[str] | None = None,
        intermediate_events: set[str] | None = None,
    ) -> Event:
        names = terminal_events or DEFAULT_TERMINAL_EVENTS
        intermediate = intermediate_events or set()
        after_seq = self._action_start_seqs.get(action_id, 0)
        deadline = time.monotonic() + timeout_s
        started = time.monotonic()
        poll_count = 0
        observed_events = 0
        observed: list[dict[str, object]] = []

        def match(events: list[Event]) -> Event | None:
            nonlocal observed_events
            for event in events:
                if event.seq <= after_seq or event.seq in self._await_consumed_event_seqs:
                    continue
                observed_events += 1
                observed.append(
                    {
                        "seq": event.seq,
                        "tick": event.tick,
                        "name": event.name,
                        "action_id": event.data.get("action_id"),
                    }
                )
                if event.name not in names or not (
                    event.data.get("action_id") == action_id
                    or event.name in GLOBAL_TERMINAL_EVENTS
                ):
                    continue
                self._await_consumed_event_seqs.add(event.seq)
                if event.name in intermediate:
                    self._record_action_intermediate(action_id, event)
                else:
                    self._finish_action_trace(
                        action_id,
                        terminal=event,
                        wait_ms=(time.monotonic() - started) * 1000.0,
                        poll_count=poll_count,
                        observed_events=observed_events,
                    )
                return event
            return None

        buffered = match(self.event_log)
        if buffered is not None:
            return buffered
        while time.monotonic() < deadline:
            execution_checkpoint()
            poll_count += 1
            event = match(self.poll_events())
            if event is not None:
                return event
            execution_checkpoint()
            time.sleep(poll_interval_s)
        raise BodyActionTimeoutError(
            f"timed out waiting for terminal event for action {action_id}",
            diagnostics={
                "action_id": action_id,
                "terminal_events": sorted(names),
                "poll_count": poll_count,
                "wait_ms": round((time.monotonic() - started) * 1000.0, 3),
                "observed_events": observed_events,
                "observed": observed[-32:],
                "last_seq": self.last_seq,
            },
        )

    def interrupt(self, reason: str | None = None) -> Result:
        return parse_result(
            self._timed_request(build_interrupt_call(self.bot_name, reason, self.app), kind="interrupt")
        )


def _transport_snapshot(transport: BodyTransport) -> dict[str, object]:
    snapshot = getattr(transport, "stats_snapshot", None)
    if not callable(snapshot):
        return {}
    value = snapshot()
    if isinstance(value, dict):
        return {"transport_stats": dict(value)}
    return {}


def _sanitize_chat_text(text: str) -> str:
    cleaned = str(text).replace("\r", " ").replace("\n", " ")
    cleaned = _MINECRAFT_FORMATTING_RE.sub("", cleaned)
    cleaned = _COORDINATE_TRIPLE_RE.sub("[position]", cleaned)
    cleaned = _BARE_COORDINATE_TRIPLE_RE.sub("[position]", cleaned)
    cleaned = _AXIS_COORDINATE_TRIPLE_RE.sub("[position]", cleaned)
    cleaned = _LABELED_SPACE_COORDINATE_TRIPLE_RE.sub("[position]", cleaned)
    cleaned = "".join(ch for ch in cleaned if ch == "\t" or ord(ch) >= 32)
    cleaned = " ".join(cleaned.strip().split())
    return cleaned[:CHAT_TEXT_LIMIT]


def _next_start(perception) -> object | None:
    return perception_next_cursor(perception)
