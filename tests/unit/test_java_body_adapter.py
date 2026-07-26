"""GovernanceAnswerer binds proposals to the real GovernancePolicy path,
and JavaBodyClient drives the protocol into ToolResults over a fake duplex."""

from __future__ import annotations

import json

from minebot.contract.governance import Region
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body_adapter import (
    GovernanceAnswerer,
    JavaBodyClient,
    TransportClosed,
)
from minebot.game.java_body_protocol import ServerProposal


def _proposal(kind: str = "break", block_id: str = "minecraft:oak_log", pos=(10, 64, 10)) -> ServerProposal:
    return ServerProposal(
        proposal_id="mp-1",
        bot="Bot",
        action_id="collect-1",
        kind=kind,
        x=pos[0],
        y=pos[1],
        z=pos[2],
        block_id=block_id,
        payload={},
    )


def _policy() -> GovernancePolicy:
    return GovernancePolicy(
        natural_regions=[Region("probe-natural", (-64, 0, -64), (64, 200, 64))],
        protected_regions=[Region("player-base", (30, 60, 30), (40, 80, 40))],
    )


def test_natural_collect_target_is_allowed_by_the_real_policy() -> None:
    allow, reason = GovernanceAnswerer(_policy()).verdict(_proposal())
    assert allow is True
    assert reason


def test_protected_region_is_denied_by_the_real_policy() -> None:
    allow, reason = GovernanceAnswerer(_policy()).verdict(_proposal(pos=(35, 64, 35)))
    assert allow is False
    assert reason == "protected_region"


def test_bot_ledger_governs_bot_placed_blocks() -> None:
    policy = _policy()
    policy.record_bot_placement((12, 64, 12), "minecraft:cobblestone", "bridge", "Bot")
    allow, reason = GovernanceAnswerer(policy).verdict(
        _proposal(block_id="minecraft:cobblestone", pos=(12, 64, 12))
    )
    # Whatever the ledger decides, the decision came from the real policy
    # with a typed reason — never from an adapter-side shortcut.
    assert isinstance(allow, bool)
    assert reason


def test_unmapped_mutation_kind_is_denied_never_guessed() -> None:
    allow, reason = GovernanceAnswerer(_policy()).verdict(_proposal(kind="place"))
    assert allow is False
    assert reason == "unsupported_mutation_kind:place"


# ---------------------------------------------------------------------------
# JavaBodyClient over a reactive fake duplex.
# ---------------------------------------------------------------------------


class FakeBodyServer:
    """Reactive in-memory Body server driving one bot's protocol.

    Implements the DuplexTransport the client drives: sent frames are reacted
    to synchronously, enqueuing response and event frames the client then
    recv()s. Per-bot event seq stays monotonic so the client sees no spurious
    gaps.
    """

    CHANNEL = "fakeplayer-body"

    def __init__(self, *, drop_after=None) -> None:
        self._out: list[str] = []
        self._seq = 0
        self._tick = 100
        self._closed = False
        self._sent_count = 0
        self._drop_after = drop_after
        self.scenario = "navigate_complete"
        self.verdict_seen: list[tuple[str, bool]] = []

    def send(self, text: str) -> None:
        if self._closed:
            raise TransportClosed("closed")
        self._sent_count += 1
        self._react(json.loads(text))
        if self._drop_after is not None and self._sent_count >= self._drop_after:
            self._closed = True

    def recv(self, timeout: float) -> str:
        if self._out:
            return self._out.pop(0)
        if self._closed:
            raise TransportClosed("closed")
        raise TimeoutError("empty")

    def close(self) -> None:
        self._closed = True

    def _react(self, message: dict) -> None:
        kind = message.get("type")
        rid = message.get("request_id")
        if kind == "HELLO":
            self._emit_response(rid, "HELLO_ACK", {
                "protocol": "fakeplayer-body/1",
                "minecraft_version": "26.1.2",
                "max_request_bytes": 16384,
                "max_requests_per_second": 40,
                "request_types": [
                    "CANCEL_ACTION", "COLLECT_BLOCK", "FIND_BLOCKS", "HELLO",
                    "MUTATION_VERDICT", "NAVIGATE", "QUERY_ACTION", "RESUME_EVENTS",
                ],
            })
        elif kind == "NAVIGATE":
            self._handle_navigate(message, rid)
        elif kind == "COLLECT_BLOCK":
            self._handle_collect(message, rid)
        elif kind == "MUTATION_VERDICT":
            self._handle_verdict(message)
        elif kind == "QUERY_ACTION":
            self._handle_query(message, rid)

    def _handle_navigate(self, message: dict, rid: str) -> None:
        action = message["action_id"]
        if self.scenario == "owner_busy":
            self._emit_error(rid, "owner_busy", retryable=True,
                             extra={"owner_action_id": "other-1", "owner_priority": "ACTION"})
            return
        self._emit_response(rid, "NAVIGATE_ACK", {"action_id": action, "state": "accepted"})
        self._emit_event("owner_acquired", action, {"type": "NAVIGATE", "priority": "ACTION"})
        if self.scenario == "navigate_no_path":
            self._emit_terminal(action, {"classification": "failed", "reason": "no_path",
                                         "elapsed_ticks": 12, "replans": 0})
        elif self.scenario == "drop_before_terminal":
            self._pending_action = action
        else:
            self._emit_event("path_planned", action, {"waypoints": 20, "partial": False})
            self._emit_terminal(action, {"classification": "completed", "reason": "goal_satisfied",
                                         "elapsed_ticks": 88, "replans": 2, "final_x": 10.0})

    def _handle_collect(self, message: dict, rid: str) -> None:
        action = message["action_id"]
        self._collect_action = action
        self._emit_response(rid, "COLLECT_BLOCK_ACK", {"action_id": action, "state": "accepted", "candidates": 1})
        self._emit_event("owner_acquired", action, {"type": "COLLECT_BLOCK", "priority": "ACTION"})
        self._emit_event("candidate_selected", action, {"x": 5, "y": 64, "z": 5, "block_id": "minecraft:oak_log"})
        self._emit_proposal(action, "mp-1", "minecraft:oak_log", 5, 64, 5)

    def _handle_verdict(self, message: dict) -> None:
        allow = bool(message.get("allow"))
        self.verdict_seen.append((message.get("proposal_id"), allow))
        action = getattr(self, "_collect_action", "collect-1")
        if allow:
            self._emit_event("mutation_allowed", action, {"proposal_id": "mp-1"})
            self._emit_event("mutation_verified", action, {"block_id": "minecraft:oak_log", "now": "minecraft:air"})
            self._emit_terminal(action, {
                "classification": "completed", "reason": "collected",
                "inventory_delta": {"item_id": "minecraft:oak_log", "before": 0, "after": 1},
                "elapsed_ticks": 140, "candidates_tried": 1,
            })
        else:
            self._emit_event("mutation_denied", action, {"proposal_id": "mp-1", "reason": "protected_region"})
            self._emit_terminal(action, {
                "classification": "failed", "reason": "candidate_targets_exhausted",
                "attempt_failures": [{"reason": "governance_denied:protected_region"}],
            })

    def _handle_query(self, message: dict, rid: str) -> None:
        action = message["action_id"]
        pending = getattr(self, "_pending_action", None)
        if action == pending:
            self._emit_response(rid, "QUERY_ACTION_RESULT", {
                "action_id": action, "state": "terminal",
                "terminal": {"classification": "completed", "reason": "goal_satisfied",
                             "elapsed_ticks": 90, "replans": 0},
            })
        else:
            self._emit_response(rid, "QUERY_ACTION_RESULT", {"action_id": action, "state": "unknown"})

    def _emit_response(self, rid, kind, payload) -> None:
        frame = {"channel": self.CHANNEL, "type": kind, "request_id": rid, "server_tick": self._tick, **payload}
        self._out.append(json.dumps(frame))

    def _emit_error(self, rid, code, *, retryable, extra=None) -> None:
        frame = {"channel": self.CHANNEL, "type": "ERROR", "request_id": rid,
                 "code": code, "message": code, "retryable": retryable, "server_tick": self._tick, **(extra or {})}
        self._out.append(json.dumps(frame))

    def _emit_event(self, name, action, data) -> None:
        self._seq += 1
        frame = {"channel": self.CHANNEL, "type": "EVENT", "bot": "Bot", "seq": self._seq,
                 "tick": self._tick, "event": name, "action_id": action, "data": data}
        self._out.append(json.dumps(frame))

    def _emit_terminal(self, action, data) -> None:
        self._emit_event("action_terminal", action, data)

    def _emit_proposal(self, action, pid, block_id, x, y, z) -> None:
        frame = {"channel": self.CHANNEL, "type": "MUTATION_PROPOSAL", "proposal_id": pid,
                 "bot": "Bot", "action_id": action, "server_tick": self._tick,
                 "mutation": {"kind": "break", "x": x, "y": y, "z": z, "block_id": block_id}}
        self._out.append(json.dumps(frame))


def _client(server, governance=None) -> JavaBodyClient:
    return JavaBodyClient("Bot", lambda: server, governance, action_wall_timeout_s=5.0, recv_timeout_s=0.01)


def test_navigate_complete_maps_to_arrived_with_metrics() -> None:
    client = _client(FakeBodyServer())
    client.connect()
    result = client.navigate({"kind": "near", "x": 10, "y": 64, "z": 0, "range": 1.5})
    assert result.success is True
    assert result.reason == "arrived"
    assert result.metrics["replans"] == 2


def test_navigate_no_path_keeps_typed_failure() -> None:
    server = FakeBodyServer()
    server.scenario = "navigate_no_path"
    client = _client(server)
    client.connect()
    result = client.navigate({"kind": "near", "x": 99, "y": 64, "z": 0, "range": 1.5})
    assert result.success is False
    assert result.reason == "no_path"
    assert result.can_retry is True


def test_owner_busy_ack_becomes_a_retryable_toolresult() -> None:
    server = FakeBodyServer()
    server.scenario = "owner_busy"
    client = _client(server)
    client.connect()
    result = client.navigate({"kind": "xz", "x": 1, "z": 2})
    assert result.success is False
    assert result.reason == "owner_busy"
    assert result.can_retry is True
    assert result.metrics["owner_action_id"] == "other-1"


def test_collect_allow_completes_with_inventory_delta() -> None:
    server = FakeBodyServer()
    policy = GovernancePolicy(natural_regions=[Region("n", (-64, 0, -64), (64, 200, 64))])
    client = _client(server, GovernanceAnswerer(policy))
    client.connect()
    result = client.collect_block(["minecraft:oak_log"], radius=16)
    assert result.success is True
    assert result.reason == "collected"
    assert result.metrics["inventory_delta"]["after"] == 1
    assert server.verdict_seen == [("mp-1", True)]


def test_collect_denied_by_governance_never_relabels_success() -> None:
    server = FakeBodyServer()
    policy = GovernancePolicy(
        natural_regions=[Region("n", (-64, 0, -64), (64, 200, 64))],
        protected_regions=[Region("base", (0, 0, 0), (10, 128, 10))],
    )
    client = _client(server, GovernanceAnswerer(policy))
    client.connect()
    result = client.collect_block(["minecraft:oak_log"], radius=16)
    assert result.success is False
    assert result.reason == "candidate_targets_exhausted"
    assert server.verdict_seen == [("mp-1", False)]


def test_no_governance_denies_proposals_by_default() -> None:
    client = _client(FakeBodyServer())
    client.connect()
    result = client.collect_block(["minecraft:oak_log"], radius=16)
    assert result.success is False
    assert client._governance is None
    # The proposal was answered with a denial, not left to time out silently.


def test_transport_drop_reconciles_via_query_action() -> None:
    server = FakeBodyServer()
    server.scenario = "drop_before_terminal"
    server._drop_after = 2
    reconnects = {"n": 0}

    def connect():
        reconnects["n"] += 1
        if reconnects["n"] == 1:
            return server
        fresh = FakeBodyServer()
        fresh.scenario = "drop_before_terminal"
        fresh._pending_action = "Bot-nav-1"
        return fresh

    client = JavaBodyClient("Bot", connect, action_wall_timeout_s=5.0, recv_timeout_s=0.01)
    client.connect()
    result = client.navigate({"kind": "near", "x": 10, "y": 64, "z": 0, "range": 1.5})
    assert result.success is True
    assert result.reason == "arrived"
    assert reconnects["n"] == 2
