"""Production adapter pieces for the ``fakeplayer-body/1`` Java Body.

``GovernanceAnswerer`` is the Tier-2 authority of the two-tier governance
contract: it binds every ``MUTATION_PROPOSAL`` to the real
:class:`~minebot.game.governance.GovernancePolicy` decision — protected
regions, bot-placement ledger, natural-region rules, and the structure-risk
assessor's authoritative voxel re-read. It never caches a permit and answers
exactly one verdict per proposal; staying silent is safe because the Java
side times a missing verdict out into a denial.

The full threaded Body-contract client lands with agent tool routing; probes
and early integrations drive :class:`~minebot.game.java_body_protocol.JavaBodyProtocol`
directly and plug this answerer into their proposal path.
"""

from __future__ import annotations

from collections import deque
import threading
import time
from typing import Callable, Protocol
from uuid import uuid4

from minebot.contract.execution import execution_checkpoint
from minebot.contract.governance import BreakContext, InteractionContext, PlaceContext
from minebot.contract.messages import ToolResult
from minebot.game.errors import BodyActionTimeoutError
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body_protocol import (
    BotEvent,
    ErrorResponse,
    EventGap,
    JavaBodyProtocol,
    MAX_TERMINAL_BYTES,
    Response,
    ServerProposal,
)


MAX_WEBSOCKET_FRAME_BYTES = 4 * MAX_TERMINAL_BYTES


class GovernanceAnswerer:
    """Answers Java Body mutation proposals with the production decision."""

    def __init__(self, policy: GovernancePolicy, *, context: BreakContext = BreakContext.COLLECT) -> None:
        self._policy = policy
        self._context = context

    def verdict(self, proposal: ServerProposal) -> tuple[bool, str]:
        if proposal.kind == "break":
            try:
                context = BreakContext(proposal.context) if proposal.context is not None else self._context
            except ValueError:
                return False, f"unsupported_break_context:{proposal.context}"
            decision = self._policy.can_break(
                (proposal.x, proposal.y, proposal.z),
                proposal.block_id,
                context,
                explicit_target=True,
                bot=proposal.bot,
                action_id=proposal.action_id,
            )
            reason = decision.reason
            confirmation = decision.details.get("governance_confirmation")
            if reason == "governance_confirmation_required" and isinstance(confirmation, dict):
                challenge_id = confirmation.get("challenge_id")
                if isinstance(challenge_id, str) and challenge_id:
                    reason = f"{reason}:{challenge_id}"
            return decision.allowed, reason
        if proposal.kind in {"open", "interact"}:
            if proposal.context is None:
                return False, "unsupported_interaction_context:None"
            try:
                context = InteractionContext(proposal.context)
            except ValueError:
                return False, f"unsupported_interaction_context:{proposal.context}"
            decision = self._policy.can_interact(
                (proposal.x, proposal.y, proposal.z),
                proposal.block_id,
                context,
            )
            return decision.allowed, decision.reason
        if proposal.kind == "place":
            if proposal.context is None:
                return False, "unsupported_place_context:None"
            try:
                context = PlaceContext(proposal.context)
            except ValueError:
                return False, f"unsupported_place_context:{proposal.context}"
            decision = self._policy.can_place(
                (proposal.x, proposal.y, proposal.z),
                proposal.block_id,
                context,
                proposal.bot,
            )
            return decision.allowed, decision.reason
        # Only mutation kinds with a mapped governance decision may pass;
        # anything else is denied, never guessed.
        return False, f"unsupported_mutation_kind:{proposal.kind}"

    def observe_event(self, event: BotEvent) -> None:
        """Commit server-verified temporary placements to the shared ledger."""

        if event.name in {"death", "respawned", "bodyMissing"}:
            self._policy.invalidate_confirmation_state()

        purpose = {
            "ascent_pillar_verified": "pillar",
        }.get(event.name)
        if purpose is None:
            return
        data = event.data
        block_id = data.get("block_id")
        coordinates = (data.get("x"), data.get("y"), data.get("z"))
        if (
            not isinstance(block_id, str)
            or not block_id
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in coordinates)
        ):
            return
        self._policy.record_bot_placement(coordinates, block_id, purpose, event.bot)

    def pending_confirmations(self, action_id: str) -> list[dict[str, object]]:
        return self._policy.pending_confirmations(action_id=action_id)

    def dismiss_action_confirmations(self, action_id: str) -> None:
        self._policy.dismiss_action_confirmations(action_id)


class DuplexTransport(Protocol):
    """Minimal blocking duplex the Java Body client drives.

    The production implementation wraps ``websockets.sync``; tests use a
    scripted in-memory duplex so the client's control flow — governance
    answers, terminals, reconnect reconciliation — is deterministic.
    """

    def send(self, text: str) -> None: ...

    def recv(self, timeout: float) -> str: ...

    def close(self) -> None: ...


class TransportClosed(Exception):
    """The duplex dropped mid-operation; the client reconnects and reconciles."""


# Java action terminal classification/reason -> Body ToolResult semantics.
# Successes map to observed truth; failures keep typed, non-relabeled reasons.
_NAVIGATE_TERMINALS: dict[str, tuple[bool, str, bool]] = {
    "completed": (True, "arrived", False),
    "canceled": (False, "canceled", False),
    "timeout": (False, "navigation_timeout", True),
}
_COLLECT_TERMINALS: dict[str, tuple[bool, str, bool]] = {
    "completed": (True, "collected", False),
    "canceled": (False, "canceled", False),
    "timeout": (False, "collect_timeout", True),
}
_DIG_DOWN_TERMINALS: dict[str, tuple[bool, str, bool]] = {
    "completed": (True, "dig_down_target_reached", False),
    "partial": (False, "dig_down_partial", True),
    "canceled": (False, "canceled", False),
    "timeout": (False, "dig_down_timeout", True),
}
_ASCEND_TERMINALS: dict[str, tuple[bool, str, bool]] = {
    "completed": (True, "surface_reached", False),
    "canceled": (False, "canceled", False),
    "timeout": (False, "ascend_timeout", True),
}
_FOLLOW_TERMINALS: dict[str, tuple[bool, str, bool]] = {
    "completed": (True, "arrived", False),
    "canceled": (False, "canceled", False),
    "timeout": (False, "follow_timeout", True),
}
_ENGAGE_TERMINALS: dict[str, tuple[bool, str, bool]] = {
    "completed": (True, "killed", False),
    "canceled": (False, "canceled", False),
    "timeout": (False, "engage_timeout", True),
}
_PLAYER_ACTION_TERMINALS: dict[str, tuple[bool, str, bool]] = {
    "completed": (True, "completed", False),
    "canceled": (False, "canceled", False),
    "timeout": (False, "action_timeout", True),
}
_CONTAINER_TRANSFER_TERMINALS = _PLAYER_ACTION_TERMINALS
_CRAFT_ITEM_TERMINALS = _PLAYER_ACTION_TERMINALS
_FURNACE_TRANSFER_TERMINALS = _PLAYER_ACTION_TERMINALS


class JavaBodyClient:
    """Blocking Body-contract provider over the ``fakeplayer-body/1`` protocol.

    One bounded objective at a time (single physical writer): a call sends its
    request, then drives the duplex — answering every ``MUTATION_PROPOSAL`` via
    the injected governance answerer and returning a :class:`ToolResult` on the
    action's single terminal. A mid-flight transport drop reconnects and
    reconciles the in-flight action through ``QUERY_ACTION`` rather than
    silently retrying a physical write.
    """

    def __init__(
        self,
        bot_name: str,
        connect: Callable[[], DuplexTransport],
        governance: GovernanceAnswerer | None = None,
        *,
        action_wall_timeout_s: float = 90.0,
        recv_timeout_s: float = 2.0,
        survival_owner: bool | None = None,
        subscribe_events: bool = True,
        terminal_query_interval_s: float = 0.5,
        terminal_delivery_grace_s: float = 5.0,
    ) -> None:
        self._bot = bot_name
        self._connect = connect
        self._governance = governance
        self._wall_timeout = action_wall_timeout_s
        self._recv_timeout = recv_timeout_s
        self._survival_owner = survival_owner
        self._subscribe_events = bool(subscribe_events)
        self._terminal_query_interval = max(0.05, float(terminal_query_interval_s))
        self._terminal_delivery_grace = max(0.1, float(terminal_delivery_grace_s))
        self._transport: DuplexTransport | None = None
        self._protocol = JavaBodyProtocol()
        self._action_counter = 0
        self._action_epoch = uuid4().hex[:12]
        self._event_gaps: list[EventGap] = []
        self._last_events: list[BotEvent] = []
        self._event_buffer: list[BotEvent] = []
        self._last_terminal: tuple[str, dict] | None = None
        self._request_times: deque[float] = deque()
        self._exchange_lock = threading.RLock()
        self._reconciliation_client: JavaBodyClient = self

    # -- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        with self._exchange_lock:
            self._discard_transport()
            self._transport = self._connect()
            self._protocol = JavaBodyProtocol()
            self._request_times.clear()
            try:
                hello = self._protocol.hello(subscribe_events=self._subscribe_events)
                hello_sent_at = time.monotonic()
                self._send(hello)
                self._await_response(
                    hello["type"],
                    request_id=hello["request_id"],
                )
                if self._protocol.max_requests_per_second > 0:
                    # HELLO is charged by the server before its response tells
                    # us the limit. Account for it in the same local window so
                    # the first burst cannot become 1 HELLO + N requests.
                    self._request_times.append(hello_sent_at)
                if self._survival_owner is not None:
                    ownership_request = self._protocol.set_survival_owner(
                        self._bot,
                        self._survival_owner,
                    )
                    self._send(ownership_request)
                    ownership = self._await_response(
                        ownership_request["type"],
                        request_id=ownership_request["request_id"],
                    )
                    if isinstance(ownership, ErrorResponse):
                        raise TransportClosed(
                            f"survival ownership negotiation failed: {ownership.code}"
                        )
            except Exception:
                self._discard_transport()
                raise

    def close(self) -> None:
        with self._exchange_lock:
            self._discard_transport()

    def _discard_transport(self) -> None:
        transport = self._transport
        self._transport = None
        if transport is None:
            return
        try:
            transport.close()
        except Exception:
            pass

    @property
    def negotiated(self) -> bool:
        return self._transport is not None and self._protocol.negotiated

    @property
    def protocol(self) -> JavaBodyProtocol:
        return self._protocol

    @property
    def event_gaps(self) -> list[EventGap]:
        return list(self._event_gaps)

    def configure_governance(self, governance: GovernanceAnswerer) -> None:
        """Bind the one production mutation authority before any objective runs."""
        if self._governance is not None and self._governance is not governance:
            raise ValueError("Java Body governance is already configured")
        self._governance = governance

    def configure_reconciliation_client(self, client: JavaBodyClient) -> None:
        """Use the provider's existing read lane for action control queries."""

        if client is self:
            return
        if client._bot != self._bot:
            raise ValueError("Java Body reconciliation client bot_name does not match")
        if self._reconciliation_client is not self and self._reconciliation_client is not client:
            raise ValueError("Java Body reconciliation client is already configured")
        self._reconciliation_client = client

    def request_response(self, build) -> Response | ErrorResponse:
        """One read-side request/response exchange.

        ``build`` is a callable ``(JavaBodyProtocol) -> dict`` so the request
        is always constructed against the current protocol epoch — a reconnect
        gets a fresh, correctly-registered request instead of a stale one.
        """
        with self._exchange_lock:
            if self._transport is None or not self._protocol.negotiated:
                self.connect()
            try:
                message = build(self._protocol)
                self._send(message)
                return self._await_response(
                    message["type"],
                    request_id=message["request_id"],
                )
            except TransportClosed:
                # Never leave a closed socket looking negotiated. Read-side
                # callers may safely retry and rebuild their request against a
                # fresh protocol epoch; mutation callers still decide their
                # own reconciliation semantics.
                self._discard_transport()
                raise

    def drain_events(self) -> list[BotEvent]:
        """Buffered pushed events since the last drain, in per-bot order."""
        with self._exchange_lock:
            drained = self._event_buffer
            self._event_buffer = []
            return drained

    def drain_event_gaps(self) -> list[EventGap]:
        with self._exchange_lock:
            drained = self._event_gaps
            self._event_gaps = []
            return drained

    def resume_events(self, after_seq: int) -> Response | ErrorResponse:
        with self._exchange_lock:
            cursor = max(0, int(after_seq))
            for attempt in range(2):
                try:
                    self._ensure_connected()
                    self._protocol.seed_last_seq(self._bot, cursor)
                    message = self._protocol.resume_events(self._bot, cursor)
                    self._send(message)
                    return self._await_response(
                        message["type"],
                        request_id=message["request_id"],
                    )
                except TransportClosed:
                    self._discard_transport()
                    if attempt > 0:
                        raise
            raise AssertionError("unreachable event-resume retry state")

    def interrupt_body(self, reason: str | None = None) -> Response | ErrorResponse:
        """Use an independent connection so cancellation can preempt a blocking action."""
        control = JavaBodyClient(
            self._bot,
            self._connect,
            self._governance,
            action_wall_timeout_s=self._wall_timeout,
            recv_timeout_s=self._recv_timeout,
            survival_owner=self._survival_owner,
            subscribe_events=False,
            terminal_query_interval_s=self._terminal_query_interval,
            terminal_delivery_grace_s=self._terminal_delivery_grace,
        )
        try:
            control.connect()
            return control.request_response(
                lambda protocol: protocol.interrupt(self._bot, reason)
            )
        finally:
            control.close()

    @property
    def last_action_events(self) -> list[BotEvent]:
        return list(self._last_events)

    @property
    def last_action_terminal(self) -> tuple[str, dict] | None:
        if self._last_terminal is None:
            return None
        action_id, terminal = self._last_terminal
        return action_id, dict(terminal)

    # -- tool-facing objectives -----------------------------------------

    def navigate(
        self,
        goal: dict,
        *,
        timeout_ticks: int | None = None,
        final_reach_distance: float | None = None,
        survival_recovery: bool = False,
        grid_radius: int | None = None,
        max_expand: int | None = None,
        allow_diagonal: bool | None = None,
        allow_ascend: bool | None = None,
        allow_descend: bool | None = None,
        allow_swim: bool | None = None,
        max_fall_depth: int | None = None,
        max_water_drop_depth: int | None = None,
        allow_break: bool = False,
        break_budget: int | None = None,
        break_context: str | None = None,
        denied_mutations: list[list[int]] | None = None,
    ) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            action_id = self._new_action_id("nav")
            request = self._protocol.navigate(
                self._bot,
                action_id,
                goal,
                timeout_ticks=timeout_ticks,
                final_reach_distance=final_reach_distance,
                survival_recovery=survival_recovery,
                grid_radius=grid_radius,
                max_expand=max_expand,
                allow_diagonal=allow_diagonal,
                allow_ascend=allow_ascend,
                allow_descend=allow_descend,
                allow_swim=allow_swim,
                max_fall_depth=max_fall_depth,
                max_water_drop_depth=max_water_drop_depth,
                allow_break=allow_break,
                break_budget=break_budget,
                break_context=break_context,
                denied_mutations=denied_mutations,
            )
            return self._run_action(request, action_id, "navigate", _NAVIGATE_TERMINALS)

    def collect_block(
        self,
        target: tuple[int, int, int],
        *,
        expected_block_id: str,
        expected_item_ids: list[str],
        excavation_budget: int = 0,
        timeout_ticks: int | None = None,
    ) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            action_id = self._new_action_id("collect")
            request = self._protocol.collect_block(
                self._bot,
                action_id,
                target,
                expected_block_id=expected_block_id,
                expected_item_ids=expected_item_ids,
                excavation_budget=excavation_budget,
                timeout_ticks=timeout_ticks,
            )
            return self._run_action(request, action_id, "collect", _COLLECT_TERMINALS)

    def dig_down(
        self,
        target_y: int,
        *,
        max_steps: int | None = None,
        max_clear_fall: int | None = None,
        timeout_ticks: int | None = None,
    ) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            action_id = self._new_action_id("dig-down")
            request = self._protocol.dig_down(
                self._bot,
                action_id,
                target_y,
                max_steps=max_steps,
                max_clear_fall=max_clear_fall,
                timeout_ticks=timeout_ticks,
            )
            return self._run_action(request, action_id, "dig_down", _DIG_DOWN_TERMINALS)

    def ascend(
        self,
        *,
        max_mutations: int | None = None,
        timeout_ticks: int | None = None,
    ) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            action_id = self._new_action_id("ascend")
            request = self._protocol.ascend(
                self._bot,
                action_id,
                max_mutations=max_mutations,
                timeout_ticks=timeout_ticks,
            )
            return self._run_action(request, action_id, "ascend", _ASCEND_TERMINALS)

    def engage_entity(self, action_id: str, params: dict) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            request = self._protocol.engage_entity(self._bot, action_id, params)
            return self._run_action(request, action_id, "engage_entity", _ENGAGE_TERMINALS)

    def follow_entity(self, action_id: str, params: dict) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            request = self._protocol.follow_entity(self._bot, action_id, params)
            return self._run_action(request, action_id, "follow_entity", _FOLLOW_TERMINALS)

    def player_action(self, action_id: str, action: str, params: dict) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            request = self._protocol.player_action(self._bot, action_id, action, params)
            return self._run_action(request, action_id, "player_action", _PLAYER_ACTION_TERMINALS)

    def container_transfer(self, action_id: str, params: dict) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            request = self._protocol.container_transfer(self._bot, action_id, params)
            return self._run_action(request, action_id, "container_transfer", _CONTAINER_TRANSFER_TERMINALS)

    def craft_item(self, action_id: str, params: dict) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            request = self._protocol.craft_item(self._bot, action_id, params)
            return self._run_action(request, action_id, "craft_item", _CRAFT_ITEM_TERMINALS)

    def furnace_transfer(self, action_id: str, params: dict) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            request = self._protocol.furnace_transfer(self._bot, action_id, params)
            return self._run_action(request, action_id, "furnace_transfer", _FURNACE_TRANSFER_TERMINALS)

    def _ensure_connected(self) -> None:
        if self._transport is None or not self._protocol.negotiated:
            self.connect()

    # -- core drive -----------------------------------------------------

    def _run_action(
        self,
        request: dict,
        action_id: str,
        kind: str,
        terminals: dict[str, tuple[bool, str, bool]],
    ) -> ToolResult:
        started = time.monotonic()
        self._last_events = []
        self._last_terminal = None
        try:
            self._send(request)
            ack = self._await_response(
                request["type"],
                request_id=request["request_id"],
            )
        except TransportClosed:
            return self._reconcile_after_drop(action_id, kind, terminals, started=started)
        if isinstance(ack, ErrorResponse):
            return ToolResult(
                success=False,
                reason=ack.code,
                can_retry=ack.retryable,
                metrics={"owner_action_id": ack.payload.get("owner_action_id")},
            )
        if ack.payload.get("state") not in ("accepted",):
            # Duplicate/running/terminal on submit: reconcile rather than guess.
            return self._reconcile_after_drop(action_id, kind, terminals, started=started)
        try:
            terminal, delivery = self._await_terminal(
                action_id,
                timeout_s=self._action_wait_timeout_s(request),
                started=started,
            )
        except TransportClosed:
            return self._reconcile_after_drop(action_id, kind, terminals, started=started)
        if terminal is None:
            return self._cancel_and_reconcile_deadline(
                action_id,
                kind,
                terminals,
                started=started,
                deadline_delivery=delivery,
            )
        self._last_terminal = (action_id, dict(terminal))
        return self._with_governance_confirmation(
            self._with_terminal_delivery(
                self._terminal_result(terminal, kind, terminals),
                delivery,
            ),
            action_id,
        )

    def _cancel_and_reconcile_deadline(
        self,
        action_id: str,
        kind: str,
        terminals: dict[str, tuple[bool, str, bool]],
        *,
        started: float,
        deadline_delivery: dict,
    ) -> ToolResult:
        """Settle a wall-expired action through the existing control/read lane."""

        control = self._reconciliation_client
        # Cancellation and terminal reconciliation are one read-lane exchange.
        # Keeping the lane lock across both prevents a BODY_STATE watcher from
        # concurrently receiving the QUERY_ACTION response on the same socket.
        with control._exchange_lock:
            try:
                reply = control.request_response(
                    lambda protocol: protocol.cancel_action(action_id)
                )
            except TransportClosed:
                raise self._deadline_timeout_error(
                    action_id,
                    kind,
                    deadline_delivery,
                    cancel_state="transport_closed",
                )
            if isinstance(reply, ErrorResponse):
                raise self._deadline_timeout_error(
                    action_id,
                    kind,
                    deadline_delivery,
                    cancel_state=reply.code,
                )
            state = str(reply.payload.get("state") or "unknown")
            terminal = reply.payload.get("terminal")
            if state == "terminal" and isinstance(terminal, dict):
                return self._deadline_cancel_terminal(
                    action_id,
                    kind,
                    terminals,
                    terminal,
                    started=started,
                    query_count=0,
                )
            if state != "cancel_requested":
                raise self._deadline_timeout_error(
                    action_id,
                    kind,
                    deadline_delivery,
                    cancel_state=state,
                )
            try:
                terminal, delivery = control._await_terminal(
                    action_id,
                    timeout_s=self._terminal_delivery_grace,
                    started=started,
                    query_immediately=True,
                )
            except TransportClosed:
                raise self._deadline_timeout_error(
                    action_id,
                    kind,
                    deadline_delivery,
                    cancel_state="query_transport_closed",
                )
        if terminal is None:
            raise self._deadline_timeout_error(
                action_id,
                kind,
                delivery,
                cancel_state="cancel_requested",
            )
        return self._deadline_cancel_terminal(
            action_id,
            kind,
            terminals,
            terminal,
            started=started,
            query_count=int(delivery.get("query_count") or 0),
        )

    def _deadline_cancel_terminal(
        self,
        action_id: str,
        kind: str,
        terminals: dict[str, tuple[bool, str, bool]],
        terminal: dict,
        *,
        started: float,
        query_count: int,
    ) -> ToolResult:
        self._last_terminal = (action_id, dict(terminal))
        result = self._terminal_result(terminal, kind, terminals)
        metrics = dict(result.metrics or {})
        metrics["deadline_cancel_requested"] = True
        result = ToolResult(
            result.success,
            result.reason,
            result.can_retry,
            result.next_suggestion,
            metrics=metrics,
        )
        return self._with_terminal_delivery(
            result,
            self._terminal_delivery("deadline_cancel_query", started, query_count),
        )

    @staticmethod
    def _deadline_timeout_error(
        action_id: str,
        kind: str,
        delivery: dict,
        *,
        cancel_state: str,
    ) -> BodyActionTimeoutError:
        return BodyActionTimeoutError(
            f"{kind} action {action_id} exceeded its wall deadline without a confirmed terminal",
            diagnostics={
                "action_id": action_id,
                "action_kind": kind,
                "terminal_delivery": dict(delivery),
                "deadline_cancel_requested": True,
                "cancel_state": cancel_state,
            },
        )

    def _with_governance_confirmation(self, result: ToolResult, action_id: str) -> ToolResult:
        if self._governance is None:
            return result
        if result.success:
            self._governance.dismiss_action_confirmations(action_id)
            return result
        confirmations = self._governance.pending_confirmations(action_id)
        if not confirmations:
            return result
        metrics = dict(result.metrics or {})
        metrics["governance_confirmation"] = confirmations[0]
        if len(confirmations) > 1:
            metrics["additional_governance_confirmations"] = confirmations[1:8]
        return ToolResult(
            success=False,
            reason="governance_confirmation_required",
            can_retry=True,
            next_suggestion=(
                "Review the bounded natural-work scope and call confirm_governance_scope with its "
                "challenge_id, or choose another candidate; hard governance denials cannot be confirmed."
            ),
            metrics=metrics,
        )

    def _action_wait_timeout_s(self, request: dict) -> float:
        timeout_ticks = request.get("timeout_ticks")
        params = request.get("params")
        if timeout_ticks is None and isinstance(params, dict):
            timeout_ticks = params.get("timeout_ticks")
        if isinstance(timeout_ticks, bool) or not isinstance(timeout_ticks, (int, float)):
            return self._wall_timeout
        declared_s = max(0.05, float(timeout_ticks) / 20.0)
        # The configured wall timeout remains a low-TPS floor. Explicitly long
        # server actions extend it rather than being cut off by the old global
        # 90-second client deadline.
        return max(self._wall_timeout, declared_s + self._terminal_delivery_grace)

    @staticmethod
    def _with_terminal_delivery(result: ToolResult, delivery: dict) -> ToolResult:
        metrics = dict(result.metrics or {})
        metrics["terminal_delivery"] = dict(delivery)
        return ToolResult(
            success=result.success,
            reason=result.reason,
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics=metrics,
        )

    def _terminal_result(
        self,
        terminal: dict,
        kind: str,
        terminals: dict[str, tuple[bool, str, bool]],
    ) -> ToolResult:
        classification = str(terminal.get("classification"))
        mapped = terminals.get(classification)
        if mapped is not None:
            success, reason, can_retry = mapped
            if success and kind in {"collect", "dig_down", "player_action", "container_transfer", "craft_item", "furnace_transfer", "engage_entity", "follow_entity"}:
                reason = str(terminal.get("reason", reason))
            return ToolResult(
                success=success,
                reason=reason if success else str(terminal.get("reason", reason)),
                can_retry=can_retry,
                metrics=self._terminal_metrics(
                    terminal,
                    include_all=kind in {"player_action", "container_transfer", "craft_item", "furnace_transfer", "engage_entity", "follow_entity"},
                ),
            )
        # Failed classification: keep the Java typed reason, never relabel.
        return ToolResult(
            success=False,
            reason=str(terminal.get("reason", "failed")),
            can_retry=_failed_is_retriable(str(terminal.get("reason", ""))),
            metrics=self._terminal_metrics(
                terminal,
                include_all=kind in {"player_action", "container_transfer", "craft_item", "furnace_transfer", "engage_entity", "follow_entity"},
            ),
        )

    @staticmethod
    def _terminal_metrics(terminal: dict, *, include_all: bool = False) -> dict:
        if include_all:
            return {
                key: value
                for key, value in terminal.items()
                if key not in {"classification", "reason"}
            }
        metrics = {
            key: terminal[key]
            for key in (
                "elapsed_ticks",
                "reason_class",
                "failure_detail",
                "goal",
                "attempts_consumed",
                "budget_remaining",
                "closest_approach_distance",
                "unloaded_chunk_count",
                "evidence_truncated",
                "evidence_omitted_fields",
                "phase_reason",
                "terminal_phase",
                "replans",
                "expanded_nodes",
                "unloaded_touches",
                "active_approach_expanded_nodes",
                "active_approach_unloaded_touches",
                "candidates_tried",
                "target_count",
                "collected_count",
                "remaining_count",
                "target_breaks",
                "mutations_completed",
                "max_candidate_attempts",
                "max_target_mutations",
                "pending_drop_count",
                "target_y",
                "steps_completed",
                "blocks_descended",
                "max_steps",
                "max_clear_fall",
                "final_feet_y",
                "final_x",
                "final_y",
                "final_z",
                "final_reach_distance",
                "ascend_steps",
                "break_steps",
                "pillar_steps",
                "pillar_fallback_from",
                "surface_route_attempted",
                "surface_route_used",
                "surface_route_replans",
                "surface_candidate_count",
                "surface_route_failure",
                "recovery_staging_attempted",
                "recovery_staging_used",
                "recovery_staging_attempts",
                "recovery_staging_candidate_count",
                "recovery_staging_candidate_total",
                "recovery_staging_best_headroom",
                "recovery_staging_frontier_y",
                "recovery_staging_replans",
                "recovery_staging_failure",
                "recovery_staging_source_failure",
                "pillar_continuation_preflight_count",
                "pillar_continuation_preflight_allowed",
                "approach_excavation_attempted",
                "approach_excavation_used",
                "approach_excavation_breaks",
                "approach_excavation_replans",
                "approach_excavation_failure",
                "approach_broken",
                "mined",
                "pickups",
                "drop_losses",
                "blocker",
                "broken",
                "placed",
                "steps",
                "paused",
                "preempted_by",
                "preempted_by_priority",
            )
            if key in terminal
        }
        if "inventory_delta" in terminal:
            metrics["inventory_delta"] = terminal["inventory_delta"]
        return metrics

    def _reconcile_after_drop(
        self,
        action_id: str,
        kind: str,
        terminals: dict[str, tuple[bool, str, bool]],
        *,
        started: float | None = None,
    ) -> ToolResult:
        started = time.monotonic() if started is None else started
        after_seq = self._protocol.last_seq(self._bot)
        try:
            self.connect()
            replay = self.resume_events(after_seq)
            if isinstance(replay, ErrorResponse):
                return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
            query = self._protocol.query_action(action_id)
            self._send(query)
            reply = self._await_response(
                query["type"],
                request_id=query["request_id"],
            )
        except TransportClosed:
            return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
        if isinstance(reply, ErrorResponse):
            return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
        state = reply.payload.get("state")
        if state == "terminal" and isinstance(reply.payload.get("terminal"), dict):
            self._last_terminal = (action_id, dict(reply.payload["terminal"]))
            return self._with_terminal_delivery(
                self._terminal_result(reply.payload["terminal"], kind, terminals),
                self._terminal_delivery("reconnect_query", started, query_count=1),
            )
        if state == "running":
            try:
                terminal, delivery = self._await_terminal(
                    action_id,
                    timeout_s=self._wall_timeout,
                    started=started,
                )
            except TransportClosed:
                return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
            if terminal is not None:
                self._last_terminal = (action_id, dict(terminal))
                return self._with_terminal_delivery(
                    self._terminal_result(terminal, kind, terminals),
                    delivery,
                )
        return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)

    def _await_response(
        self,
        request_type: str,
        *,
        request_id: str | None = None,
    ) -> Response | ErrorResponse:
        import time

        deadline = time.monotonic() + self._wall_timeout
        while time.monotonic() < deadline:
            for item in self._pump_once():
                if isinstance(item, Response):
                    if request_id is not None and item.request_id != request_id:
                        continue
                    if request_id is None and item.request_type != request_type:
                        continue
                    return item
                if isinstance(item, ErrorResponse):
                    # A request-less transport error applies to the active
                    # exchange. A late application response belongs to the
                    # abandoned request whose id it carries and must not be
                    # reinterpreted as this request's payload.
                    if item.request_id is None:
                        return item
                    if request_id is not None and item.request_id != request_id:
                        continue
                    if request_id is None and item.request_type != request_type:
                        continue
                    return item
        suffix = "" if request_id is None else f" ({request_id})"
        raise TransportClosed(f"no response to {request_type}{suffix}")

    def _await_terminal(
        self,
        action_id: str,
        *,
        timeout_s: float,
        started: float,
        query_immediately: bool = False,
    ) -> tuple[dict | None, dict]:
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        next_query = (
            time.monotonic()
            if query_immediately
            else time.monotonic() + self._terminal_query_interval
        )
        pending_query_id: str | None = None
        pending_query_started = 0.0
        query_count = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if pending_query_id is None and now >= next_query:
                query = self._protocol.query_action(action_id)
                self._send(query)
                pending_query_id = str(query["request_id"])
                pending_query_started = now
                query_count += 1
            if pending_query_id is not None and now - pending_query_started > max(2.0, self._recv_timeout * 2.0):
                raise TransportClosed("no QUERY_ACTION response during terminal wait")
            wake_in = max(0.01, next_query - now) if pending_query_id is None else self._recv_timeout
            recv_timeout = min(self._recv_timeout, wake_in, max(0.01, deadline - now))
            for item in self._pump_once(timeout_s=recv_timeout):
                if isinstance(item, BotEvent):
                    self._last_events.append(item)
                    if item.name == "action_terminal" and item.action_id == action_id:
                        return item.data, self._terminal_delivery("event", started, query_count)
                elif isinstance(item, Response) and item.request_id == pending_query_id:
                    pending_query_id = None
                    state = item.payload.get("state")
                    terminal = item.payload.get("terminal")
                    if state == "terminal" and isinstance(terminal, dict):
                        return terminal, self._terminal_delivery("query", started, query_count)
                    if state == "unknown":
                        raise TransportClosed("accepted action became unknown during terminal wait")
                    next_query = time.monotonic() + self._terminal_query_interval
                elif isinstance(item, ErrorResponse) and item.request_id == pending_query_id:
                    raise TransportClosed(f"QUERY_ACTION failed: {item.code}")
        return None, self._terminal_delivery("deadline", started, query_count)

    @staticmethod
    def _terminal_delivery(source: str, started: float, query_count: int) -> dict:
        return {
            "source": source,
            "action_wall_ms": round(max(0.0, time.monotonic() - started) * 1000.0, 3),
            "query_count": int(query_count),
        }

    def _pump_once(self, *, timeout_s: float | None = None):
        assert self._transport is not None
        import json as _json

        execution_checkpoint()
        try:
            frame = self._transport.recv(
                self._recv_timeout if timeout_s is None else max(0.001, float(timeout_s))
            )
        except TimeoutError:
            return []
        except TransportClosed:
            raise
        except Exception as error:  # transport-specific close/errors
            raise TransportClosed(str(error)) from error
        items = self._protocol.feed(_json.loads(frame))
        out: list = []
        for item in items:
            if isinstance(item, ServerProposal):
                self._answer_proposal(item)
            elif isinstance(item, EventGap):
                self._event_gaps.append(item)
            else:
                if isinstance(item, BotEvent):
                    if self._governance is not None:
                        self._governance.observe_event(item)
                    self._event_buffer.append(item)
                out.append(item)
        return out

    def _answer_proposal(self, proposal: ServerProposal) -> None:
        # No governance = deny by default (the Java side also times out to a
        # denial, but answering promptly is cheaper and clearer).
        if self._governance is None:
            allow, reason = False, "no_governance_configured"
        else:
            allow, reason = self._governance.verdict(proposal)
        self._send(self._protocol.mutation_verdict(proposal.proposal_id, allow, reason))

    def _send(self, message: dict) -> None:
        import json as _json

        if self._transport is None:
            raise TransportClosed("transport is not connected")
        try:
            self._pace_request()
            self._transport.send(_json.dumps(message))
        except Exception as error:
            raise TransportClosed(str(error)) from error

    def _pace_request(self) -> None:
        """Honor the server-advertised request window before sending."""

        limit = self._protocol.max_requests_per_second
        if limit <= 0:
            return
        while True:
            now = time.monotonic()
            while self._request_times and now - self._request_times[0] >= 1.0:
                self._request_times.popleft()
            if len(self._request_times) < limit:
                self._request_times.append(now)
                return
            time.sleep(max(0.001, 1.0 - (now - self._request_times[0]) + 0.001))

    def _new_action_id(self, prefix: str) -> str:
        self._action_counter += 1
        return f"{self._bot}-{prefix}-{self._action_epoch}-{self._action_counter}"


def _failed_is_retriable(reason: str) -> bool:
    # Terrain/coverage failures may improve with a different target or state;
    # governance denials and exhaustions are terminal for this attempt.
    non_retriable = (
        "governance_denied",
        "governance_blocked",
        "unreachable_in_current_capabilities",
    )
    return not any(reason.startswith(tag) for tag in non_retriable)


def websocket_transport(url: str, *, open_timeout: float = 10.0) -> Callable[[], DuplexTransport]:
    """A production DuplexTransport factory backed by ``websockets.sync``.

    Returned as a factory so :class:`JavaBodyClient` can reconnect by calling
    it again. Import is local so the sans-io protocol core stays dependency-free.
    """

    def connect() -> DuplexTransport:
        from websockets.sync.client import connect as ws_connect

        socket = ws_connect(
            url,
            open_timeout=open_timeout,
            max_size=MAX_WEBSOCKET_FRAME_BYTES,
        )

        class _WebSocketDuplex:
            def send(self, text: str) -> None:
                socket.send(text)

            def recv(self, timeout: float) -> str:
                return socket.recv(timeout=timeout)

            def close(self) -> None:
                socket.close()

        return _WebSocketDuplex()

    return connect
