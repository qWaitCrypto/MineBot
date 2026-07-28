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

from minebot.contract.governance import BreakContext, InteractionContext, PlaceContext
from minebot.contract.messages import ToolResult
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body_protocol import (
    BotEvent,
    ErrorResponse,
    EventGap,
    JavaBodyProtocol,
    Response,
    ServerProposal,
)


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
            )
            return decision.allowed, decision.reason
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
    ) -> None:
        self._bot = bot_name
        self._connect = connect
        self._governance = governance
        self._wall_timeout = action_wall_timeout_s
        self._recv_timeout = recv_timeout_s
        self._survival_owner = survival_owner
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

    # -- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        with self._exchange_lock:
            self._transport = self._connect()
            self._protocol = JavaBodyProtocol()
            self._request_times.clear()
            self._send(self._protocol.hello())
            self._await_response("HELLO")
            if self._survival_owner is not None:
                self._send(self._protocol.set_survival_owner(
                    self._bot, self._survival_owner
                ))
                ownership = self._await_response("SET_SURVIVAL_OWNER")
                if isinstance(ownership, ErrorResponse):
                    raise TransportClosed(
                        f"survival ownership negotiation failed: {ownership.code}"
                    )

    def close(self) -> None:
        with self._exchange_lock:
            if self._transport is not None:
                self._transport.close()
                self._transport = None

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

    def request_response(self, build) -> Response | ErrorResponse:
        """One read-side request/response exchange.

        ``build`` is a callable ``(JavaBodyProtocol) -> dict`` so the request
        is always constructed against the current protocol epoch — a reconnect
        gets a fresh, correctly-registered request instead of a stale one.
        """
        with self._exchange_lock:
            if self._transport is None or not self._protocol.negotiated:
                self.connect()
            message = build(self._protocol)
            self._send(message)
            return self._await_response(message["type"])

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
            self._ensure_connected()
            self._protocol.seed_last_seq(self._bot, max(0, int(after_seq)))
            return self.request_response(
                lambda protocol: protocol.resume_events(self._bot, max(0, int(after_seq)))
            )

    def interrupt_body(self, reason: str | None = None) -> Response | ErrorResponse:
        """Use an independent connection so cancellation can preempt a blocking action."""
        control = JavaBodyClient(
            self._bot,
            self._connect,
            self._governance,
            action_wall_timeout_s=self._wall_timeout,
            recv_timeout_s=self._recv_timeout,
            survival_owner=self._survival_owner,
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
            )
            return self._run_action(request, action_id, "navigate", _NAVIGATE_TERMINALS)

    def collect_block(
        self,
        block_ids: list[str],
        *,
        radius: int | None = None,
        vertical_radius: int | None = None,
        timeout_ticks: int | None = None,
    ) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            action_id = self._new_action_id("collect")
            request = self._protocol.collect_block(
                self._bot,
                action_id,
                block_ids,
                radius=radius,
                vertical_radius=vertical_radius,
                timeout_ticks=timeout_ticks,
            )
            return self._run_action(request, action_id, "collect", _COLLECT_TERMINALS)

    def ascend(
        self,
        *,
        target_y: int | None = None,
        timeout_ticks: int | None = None,
    ) -> ToolResult:
        with self._exchange_lock:
            self._ensure_connected()
            action_id = self._new_action_id("ascend")
            request = self._protocol.ascend(
                self._bot,
                action_id,
                target_y=target_y,
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
        self._last_events = []
        self._last_terminal = None
        try:
            self._send(request)
            ack = self._await_response(request["type"])
        except TransportClosed:
            return self._reconcile_after_drop(action_id, kind, terminals)
        if isinstance(ack, ErrorResponse):
            return ToolResult(
                success=False,
                reason=ack.code,
                can_retry=ack.retryable,
                metrics={"owner_action_id": ack.payload.get("owner_action_id")},
            )
        if ack.payload.get("state") not in ("accepted",):
            # Duplicate/running/terminal on submit: reconcile rather than guess.
            return self._reconcile_after_drop(action_id, kind, terminals)
        try:
            terminal = self._await_terminal(action_id)
        except TransportClosed:
            return self._reconcile_after_drop(action_id, kind, terminals)
        if terminal is None:
            return ToolResult(success=False, reason=f"{kind}_no_terminal", can_retry=True)
        self._last_terminal = (action_id, dict(terminal))
        return self._terminal_result(terminal, kind, terminals)

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
            if success and kind in {"collect", "player_action", "container_transfer", "craft_item", "furnace_transfer", "engage_entity", "follow_entity"}:
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
                "replans",
                "expanded_nodes",
                "unloaded_touches",
                "candidates_tried",
                "final_x",
                "final_y",
                "final_z",
                "final_reach_distance",
                "target_y",
                "ascend_steps",
                "break_steps",
                "pillar_steps",
                "pillar_fallback_from",
                "broken",
                "placed",
                "paused",
                "preempted_by",
                "preempted_by_priority",
            )
            if key in terminal
        }
        if "inventory_delta" in terminal:
            metrics["inventory_delta"] = terminal["inventory_delta"]
        if "attempt_failures" in terminal:
            metrics["attempt_failures"] = terminal["attempt_failures"]
        return metrics

    def _reconcile_after_drop(
        self,
        action_id: str,
        kind: str,
        terminals: dict[str, tuple[bool, str, bool]],
    ) -> ToolResult:
        after_seq = self._protocol.last_seq(self._bot)
        try:
            self.connect()
            replay = self.resume_events(after_seq)
            if isinstance(replay, ErrorResponse):
                return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
            self._send(self._protocol.query_action(action_id))
            reply = self._await_response("QUERY_ACTION")
        except TransportClosed:
            return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
        if isinstance(reply, ErrorResponse):
            return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
        state = reply.payload.get("state")
        if state == "terminal" and isinstance(reply.payload.get("terminal"), dict):
            self._last_terminal = (action_id, dict(reply.payload["terminal"]))
            return self._terminal_result(reply.payload["terminal"], kind, terminals)
        if state == "running":
            try:
                terminal = self._await_terminal(action_id)
            except TransportClosed:
                return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
            if terminal is not None:
                self._last_terminal = (action_id, dict(terminal))
                return self._terminal_result(terminal, kind, terminals)
        return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)

    def _await_response(self, request_type: str) -> Response | ErrorResponse:
        import time

        deadline = time.monotonic() + self._wall_timeout
        while time.monotonic() < deadline:
            for item in self._pump_once():
                if isinstance(item, (Response, ErrorResponse)):
                    return item
        raise TransportClosed(f"no response to {request_type}")

    def _await_terminal(self, action_id: str) -> dict | None:
        import time

        deadline = time.monotonic() + self._wall_timeout
        while time.monotonic() < deadline:
            for item in self._pump_once():
                if isinstance(item, BotEvent):
                    self._last_events.append(item)
                    if item.name == "action_terminal" and item.action_id == action_id:
                        return item.data
        return None

    def _pump_once(self):
        assert self._transport is not None
        import json as _json

        try:
            frame = self._transport.recv(self._recv_timeout)
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
    non_retriable = ("governance_denied", "target_not_found")
    return not any(reason.startswith(tag) for tag in non_retriable)


def websocket_transport(url: str, *, open_timeout: float = 10.0) -> Callable[[], DuplexTransport]:
    """A production DuplexTransport factory backed by ``websockets.sync``.

    Returned as a factory so :class:`JavaBodyClient` can reconnect by calling
    it again. Import is local so the sans-io protocol core stays dependency-free.
    """

    def connect() -> DuplexTransport:
        from websockets.sync.client import connect as ws_connect

        socket = ws_connect(url, open_timeout=open_timeout)

        class _WebSocketDuplex:
            def send(self, text: str) -> None:
                socket.send(text)

            def recv(self, timeout: float) -> str:
                return socket.recv(timeout=timeout)

            def close(self) -> None:
                socket.close()

        return _WebSocketDuplex()

    return connect
