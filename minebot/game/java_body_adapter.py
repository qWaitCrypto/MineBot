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

from typing import Callable, Protocol

from minebot.contract.governance import BreakContext
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
        if proposal.kind != "break":
            # Only mutation kinds with a mapped governance decision may pass;
            # anything else is denied, never guessed.
            return False, f"unsupported_mutation_kind:{proposal.kind}"
        decision = self._policy.can_break(
            (proposal.x, proposal.y, proposal.z),
            proposal.block_id,
            self._context,
            explicit_target=True,
        )
        return decision.allowed, decision.reason


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
    ) -> None:
        self._bot = bot_name
        self._connect = connect
        self._governance = governance
        self._wall_timeout = action_wall_timeout_s
        self._recv_timeout = recv_timeout_s
        self._transport: DuplexTransport | None = None
        self._protocol = JavaBodyProtocol()
        self._action_counter = 0
        self._event_gaps: list[EventGap] = []
        self._last_events: list[BotEvent] = []
        self._event_buffer: list[BotEvent] = []

    # -- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        self._transport = self._connect()
        self._protocol = JavaBodyProtocol()
        self._send(self._protocol.hello())
        self._await_response("HELLO")

    def close(self) -> None:
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

    def request_response(self, build) -> Response | ErrorResponse:
        """One read-side request/response exchange.

        ``build`` is a callable ``(JavaBodyProtocol) -> dict`` so the request
        is always constructed against the current protocol epoch — a reconnect
        gets a fresh, correctly-registered request instead of a stale one.
        """
        if self._transport is None or not self._protocol.negotiated:
            self.connect()
        message = build(self._protocol)
        self._send(message)
        return self._await_response(message["type"])

    def drain_events(self) -> list[BotEvent]:
        """Buffered pushed events since the last drain, in per-bot order."""
        drained = self._event_buffer
        self._event_buffer = []
        return drained

    @property
    def last_action_events(self) -> list[BotEvent]:
        return list(self._last_events)

    # -- tool-facing objectives -----------------------------------------

    def navigate(self, goal: dict, *, timeout_ticks: int | None = None) -> ToolResult:
        action_id = self._new_action_id("nav")
        request = self._protocol.navigate(self._bot, action_id, goal, timeout_ticks=timeout_ticks)
        return self._run_action(request, action_id, "navigate", _NAVIGATE_TERMINALS)

    def collect_block(
        self,
        block_ids: list[str],
        *,
        radius: int | None = None,
        vertical_radius: int | None = None,
        timeout_ticks: int | None = None,
    ) -> ToolResult:
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

    # -- core drive -----------------------------------------------------

    def _run_action(
        self,
        request: dict,
        action_id: str,
        kind: str,
        terminals: dict[str, tuple[bool, str, bool]],
    ) -> ToolResult:
        self._last_events = []
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
            if success:
                reason = str(terminal.get("reason", reason)) if kind == "collect" else reason
            return ToolResult(
                success=success,
                reason=reason if success else str(terminal.get("reason", reason)),
                can_retry=can_retry,
                metrics=self._terminal_metrics(terminal),
            )
        # Failed classification: keep the Java typed reason, never relabel.
        return ToolResult(
            success=False,
            reason=str(terminal.get("reason", "failed")),
            can_retry=_failed_is_retriable(str(terminal.get("reason", ""))),
            metrics=self._terminal_metrics(terminal),
        )

    @staticmethod
    def _terminal_metrics(terminal: dict) -> dict:
        metrics = {
            key: terminal[key]
            for key in ("elapsed_ticks", "replans", "expanded_nodes", "unloaded_touches", "candidates_tried")
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
        try:
            self.connect()
            self._send(self._protocol.query_action(action_id))
            reply = self._await_response("QUERY_ACTION")
        except TransportClosed:
            return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
        if isinstance(reply, ErrorResponse):
            return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
        state = reply.payload.get("state")
        if state == "terminal" and isinstance(reply.payload.get("terminal"), dict):
            return self._terminal_result(reply.payload["terminal"], kind, terminals)
        if state == "running":
            try:
                terminal = self._await_terminal(action_id)
            except TransportClosed:
                return ToolResult(success=False, reason="action_reconciliation_unknown", can_retry=True)
            if terminal is not None:
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
            self._transport.send(_json.dumps(message))
        except Exception as error:
            raise TransportClosed(str(error)) from error

    def _new_action_id(self, prefix: str) -> str:
        self._action_counter += 1
        return f"{self._bot}-{prefix}-{self._action_counter}"


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
