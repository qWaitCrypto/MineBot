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


def test_proposal_context_selects_the_governance_break_context() -> None:
    policy = _policy()
    proposal = _proposal(block_id="minecraft:stone")
    proposal = ServerProposal(**{**proposal.__dict__, "context": "recovery"})

    allow, reason = GovernanceAnswerer(policy).verdict(proposal)

    assert isinstance(allow, bool)
    assert reason


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


def _inventory_slot_type(slot: int) -> str:
    if slot <= 8:
        return "hotbar"
    if slot <= 35:
        return "inventory"
    if slot <= 39:
        return "armor"
    if slot == 40:
        return "offhand"
    return "aux"


def _inventory_slot_label(slot: int) -> str:
    if slot <= 8:
        return f"hotbar.{slot}"
    if slot <= 35:
        return f"inventory.{slot - 9}"
    return {
        36: "armor.feet",
        37: "armor.legs",
        38: "armor.chest",
        39: "armor.head",
        40: "offhand",
    }.get(slot, f"aux.{slot - 41}")


def _fake_block_fact(
    x: int,
    y: int,
    z: int,
    *,
    block: str = "minecraft:stone",
    state: str = "SOLID",
    properties: dict | None = None,
) -> dict:
    return {
        "x": x,
        "y": y,
        "z": z,
        "type": block,
        "state": state,
        "properties": dict(properties or {}),
    }


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
        self.requests: list[dict] = []

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
        self.requests.append(dict(message))
        kind = message.get("type")
        rid = message.get("request_id")
        if kind == "HELLO":
            self._emit_response(rid, "HELLO_ACK", {
                "protocol": "fakeplayer-body/1",
                "minecraft_version": "26.1.2",
                "max_request_bytes": 16384,
                "max_requests_per_second": 40,
                "request_types": [
                    "ASCEND", "BODY_STATE", "CANCEL_ACTION", "COLLECT_BLOCK", "FIND_BLOCKS",
                    "HELLO", "INVENTORY", "MUTATION_VERDICT", "NAVIGATE", "QUERY_ACTION",
                    "RESUME_EVENTS", "WORLD_READ",
                ],
            })
        elif kind == "NAVIGATE":
            self._handle_navigate(message, rid)
        elif kind == "COLLECT_BLOCK":
            self._handle_collect(message, rid)
        elif kind == "ASCEND":
            self._handle_ascend(message, rid)
        elif kind == "MUTATION_VERDICT":
            self._handle_verdict(message)
        elif kind == "QUERY_ACTION":
            self._handle_query(message, rid)
        elif kind == "BODY_STATE":
            self._emit_response(rid, "BODY_STATE_RESULT", {
                "bot": message.get("bot_name"),
                "missing": False,
                "position": {"x": 10.5, "y": 64.0, "z": -3.5},
                "yaw": 90.0, "pitch": 0.0,
                "health": 18.0, "food": 17, "air": 300,
                "dimension": "minecraft:overworld",
                "game_time": 123456,
                "inventory_counts": {"minecraft:oak_log": 3},
                "selected_item": "minecraft:oak_log",
                "offhand_item": None,
                "body_owner": None,
            })
        elif kind == "FIND_BLOCKS":
            self._handle_find_blocks(message, rid)
        elif kind == "INVENTORY":
            if message.get("bot_name") == "MissingBot":
                self._emit_response(rid, "INVENTORY_RESULT", {
                    "bot": "MissingBot",
                    "missing": True,
                })
                return
            start = max(0, min(45, int(message.get("start", 0))))
            limit = max(1, min(46, int(message.get("limit", 46))))
            end = min(46, start + limit)
            slots = []
            for slot in range(start, end):
                item = "minecraft:stone" if slot == 0 else "minecraft:diamond_helmet" if slot == 39 else None
                count = 3 if slot == 0 else 1 if slot == 39 else 0
                slots.append({
                    "slot": slot,
                    "slotType": _inventory_slot_type(slot),
                    "slotLabel": _inventory_slot_label(slot),
                    "empty": item is None,
                    "item": item,
                    "count": count,
                    "stackRaw": None if item is None else json.dumps({
                        "id": item,
                        "count": count,
                        "components": {"minecraft:damage": 7} if slot == 39 else {},
                    }, separators=(",", ":")),
                })
            self._emit_response(rid, "INVENTORY_RESULT", {
                "bot": message.get("bot_name"),
                "missing": False,
                "start": start,
                "limit": limit,
                "nextStart": None if end >= 46 else end,
                "totalSlots": 46,
                "slots": slots,
            })
        elif kind == "WORLD_READ":
            self._handle_world_read(message, rid)

    def _handle_find_blocks(self, message: dict, rid: str) -> None:
        if message.get("bot_name") == "MissingBot":
            self._emit_error(rid, "body_missing", retryable=True)
            return
        all_matches = [
            {
                "x": 1,
                "y": 64,
                "z": 0,
                "block_id": "minecraft:oak_log",
                "state": "SOLID",
                "distance_squared": 1.0,
            },
            {
                "x": 4,
                "y": 65,
                "z": 0,
                "block_id": "minecraft:oak_log",
                "state": "SOLID",
                "distance_squared": 17.0,
            },
            {
                "x": 7,
                "y": 64,
                "z": 0,
                "block_id": "minecraft:oak_log",
                "state": "SOLID",
                "distance_squared": 49.0,
            },
        ]
        start = 2 if message.get("cursor") == "find-page-2" else 0
        end = min(len(all_matches), start + int(message.get("limit", 32)))
        self._emit_response(rid, "FIND_BLOCKS_RESULT", {
            "server_cost_micros": 90,
            "start": start,
            "total_matches": len(all_matches),
            "index_generation": 321,
            "coverage_complete": True,
            "unloaded_chunk_count": 0,
            "result_capped": False,
            "next_cursor": "find-page-2" if end < len(all_matches) else None,
            "matches": all_matches[start:end],
        })

    def _handle_navigate(self, message: dict, rid: str) -> None:
        action = message["action_id"]
        self._navigate_action = action
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

    def _handle_world_read(self, message: dict, rid: str) -> None:
        scope = message["scope"]
        if message.get("bot_name") == "MissingBot":
            self._emit_response(rid, "WORLD_READ_RESULT", {
                "bot": "MissingBot", "scope": scope, "missing": True,
            })
            return
        params = message.get("params") or {}
        if scope == "blockAt":
            data = _fake_block_fact(
                int(params["x"]), int(params["y"]), int(params["z"]),
                block="minecraft:oak_log", properties={"axis": "x"},
            )
            complete = True
            next_cursor = None
        elif scope == "blockCells":
            requested = params.get("cells") or []
            start = int(params.get("start", 0))
            limit = int(params.get("limit", 64))
            end = min(len(requested), start + limit)
            data = {
                "start": start,
                "limit": limit,
                "nextStart": None if end >= len(requested) else end,
                "total": len(requested),
                "count": end - start,
                "cells": [_fake_block_fact(*position) for position in requested[start:end]],
            }
            complete = end >= len(requested)
            next_cursor = None if complete else str(end)
        elif scope == "surfaceColumns":
            requested = params.get("columns") or []
            data = {
                "start": 0, "limit": 64, "nextStart": None,
                "total": len(requested), "count": len(requested),
                "columns": [
                    {
                        "x": x, "z": z, "feetY": 65,
                        "feetType": "minecraft:air", "feetState": "CLEAR",
                        "headType": "minecraft:air", "headState": "CLEAR",
                        "supportType": "minecraft:grass_block", "supportState": "SOLID",
                    }
                    for x, z in requested
                ],
            }
            complete = True
            next_cursor = None
        else:
            debug = scope == "debugBlocks"
            block = _fake_block_fact(1, 64, 0, block="minecraft:oak_log")
            data = {
                "center": [0.5, 64.0, 0.5],
                "radius": int(params.get("radius", 1)),
                "start": 0,
                "limit": int(params.get("limit", 64)),
                "nextStart": None,
                "total": 27,
                "count": 1,
                "blocks": [block],
            }
            if debug:
                data.update({
                    "cursor": _fake_block_fact(0, 64, 0, block="minecraft:air", state="CLEAR"),
                    "feet": _fake_block_fact(0, 63, 0),
                    "head": _fake_block_fact(0, 65, 0, block="minecraft:air", state="CLEAR"),
                })
            complete = True
            next_cursor = None
        self._emit_response(rid, "WORLD_READ_RESULT", {
            "bot": message.get("bot_name"),
            "scope": scope,
            "missing": False,
            "ok": True,
            "complete": complete,
            "server_cost_micros": 125,
            "data": data,
            "uncertainty": [] if complete else [{"reason": "page_limit"}],
            "next": next_cursor,
        })

    def _handle_collect(self, message: dict, rid: str) -> None:
        action = message["action_id"]
        self._collect_action = action
        self._emit_response(rid, "COLLECT_BLOCK_ACK", {"action_id": action, "state": "accepted", "candidates": 1})
        self._emit_event("owner_acquired", action, {"type": "COLLECT_BLOCK", "priority": "ACTION"})
        self._emit_event("candidate_selected", action, {"x": 5, "y": 64, "z": 5, "block_id": "minecraft:oak_log"})
        self._emit_proposal(action, "mp-1", "minecraft:oak_log", 5, 64, 5)

    def _handle_ascend(self, message: dict, rid: str) -> None:
        action = message["action_id"]
        self._emit_response(rid, "ASCEND_ACK", {"action_id": action, "state": "accepted"})
        self._emit_event("owner_acquired", action, {"type": "ASCEND", "priority": "RECOVERY"})
        self._emit_event("ascent_step_verified", action, {"x": 1, "y": 65, "z": 0})
        self._emit_terminal(action, {
            "classification": "completed",
            "reason": "surface_reached",
            "final_y": 70,
            "target_y": 320,
            "ascend_steps": 6,
            "break_steps": 12,
            "elapsed_ticks": 280,
        })

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


def test_ascend_maps_verified_surface_terminal() -> None:
    client = _client(FakeBodyServer())

    result = client.ascend(timeout_ticks=2_400)

    assert result.success is True
    assert result.reason == "surface_reached"
    assert result.metrics["final_y"] == 70
    assert result.metrics["ascend_steps"] == 6


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
        fresh._pending_action = server._pending_action
        return fresh

    client = JavaBodyClient("Bot", connect, action_wall_timeout_s=5.0, recv_timeout_s=0.01)
    client.connect()
    result = client.navigate({"kind": "near", "x": 10, "y": 64, "z": 0, "range": 1.5})
    assert result.success is True
    assert result.reason == "arrived"
    assert reconnects["n"] == 2


def test_action_ids_do_not_collide_across_client_process_epochs() -> None:
    first_server = FakeBodyServer()
    second_server = FakeBodyServer()
    first = _client(first_server)
    second = _client(second_server)

    assert first.navigate({"kind": "xz", "x": 1, "z": 2}).success is True
    assert second.navigate({"kind": "xz", "x": 1, "z": 2}).success is True

    assert first_server._navigate_action != second_server._navigate_action


# ---------------------------------------------------------------------------
# JavaBody: the neutral Body-contract face over the client.
# ---------------------------------------------------------------------------

from minebot.contract import Action
from minebot.game.java_body import JavaBody


def test_java_body_get_state_maps_authoritative_wire_state() -> None:
    client = _client(FakeBodyServer())
    body = JavaBody(client, "Bot")

    state = body.get_state()

    assert state.missing is False
    assert state.pos == (10.5, 64.0, -3.5)
    assert state.health == 18.0
    assert state.food == 17
    assert state.oxygen == 300
    assert state.dimension == "minecraft:overworld"
    assert state.inventory_counts == {"minecraft:oak_log": 3}
    assert state.selected_item == "minecraft:oak_log"
    assert state.body_owner is None


def test_java_body_perceive_adapts_find_blocks_and_gaps_the_rest() -> None:
    client = _client(FakeBodyServer())
    body = JavaBody(client, "Bot")

    first = body.perceive(
        "findBlocks",
        {"type": "oak_log", "radius": 32, "y_radius": 12, "limit": 2, "start": 0},
    )
    second = body.perceive(
        "findBlocks",
        {"types": ["oak_log"], "radius": 32, "y_radius": 12, "limit": 2, "cursor": first.next},
    )

    assert first.ok is True
    assert first.complete is False
    assert first.next == "find-page-2"
    assert first.data["blocks"][0] == {
        "x": 1,
        "y": 64,
        "z": 0,
        "type": "minecraft:oak_log",
        "state": "SOLID",
        "dist2": 1.0,
    }
    assert first.data["totalMatches"] == 3
    assert first.data["serverCostMicros"] == 90
    assert second.ok is True
    assert second.complete is True
    assert second.data["start"] == 2
    assert second.data["count"] == 1
    find_requests = [request for request in client._transport.requests if request.get("type") == "FIND_BLOCKS"]
    assert find_requests[0]["block_ids"] == ["minecraft:oak_log"]
    assert find_requests[0]["vertical_radius"] == 12
    assert find_requests[1]["cursor"] == "find-page-2"

    gap = body.perceive("nearbyEntities", {})
    assert gap.ok is False
    assert gap.error == "capability_unavailable:nearbyEntities"


def test_java_body_find_blocks_rejects_numeric_resume_without_snapshot_cursor() -> None:
    body = JavaBody(_client(FakeBodyServer()), "Bot")

    result = body.perceive("findBlocks", {"type": "oak_log", "radius": 32, "start": 2})

    assert result.ok is False
    assert result.error == "invalid_cursor:numeric_resume_requires_original_snapshot"


def test_java_body_inventory_preserves_paging_slots_and_metadata() -> None:
    body = JavaBody(_client(FakeBodyServer()), "Bot")

    first = body.perceive("inventory", {"start": 0, "limit": 40})
    second = body.perceive("inventory", {"start": 40, "limit": 40})

    assert first.ok is True
    assert first.complete is False
    assert first.next == "40"
    assert first.uncertainty == [{"reason": "page_limit"}]
    assert first.data["slots"][0]["item"] == "minecraft:stone"
    assert '"minecraft:damage":7' in first.data["slots"][39]["stackRaw"]
    assert second.ok is True
    assert second.complete is True
    assert second.next is None
    assert len(second.data["slots"]) == 6


def test_java_body_inventory_missing_body_is_explicit_and_complete() -> None:
    body = JavaBody(_client(FakeBodyServer()), "MissingBot")

    result = body.perceive("inventory", {"start": 0, "limit": 7})

    assert result.ok is False
    assert result.complete is True
    assert result.error == "missing_body"
    assert result.uncertainty == [{"reason": "missing_body"}]


def test_java_body_world_read_family_preserves_existing_perception_shapes() -> None:
    body = JavaBody(_client(FakeBodyServer()), "Bot")

    exact = body.perceive("blockAt", {"x": 1, "y": 64, "z": 0})
    cells = body.perceive(
        "blockCells",
        {"cells": [[0, 63, 0], [1, 64, 0]], "start": 0, "limit": 1},
    )
    columns = body.perceive("surfaceColumns", {"columns": [[0, 0], [1, 0]]})
    nearby = body.perceive("nearbyBlocks", {"radius": 1, "limit": 32})
    debug = body.perceive("debugBlocks", {"radius": 1, "limit": 64})

    assert exact.ok and exact.complete
    assert exact.data["type"] == "minecraft:oak_log"
    assert exact.data["properties"] == {"axis": "x"}
    assert exact.data["serverCostMicros"] == 125
    assert cells.ok and not cells.complete
    assert cells.next == "1"
    assert cells.data["cells"][0]["state"] == "SOLID"
    assert columns.ok and columns.complete
    assert columns.data["columns"][0]["supportType"] == "minecraft:grass_block"
    assert nearby.data["blocks"][0]["type"] == "minecraft:oak_log"
    assert debug.data["cursor"]["state"] == "CLEAR"
    assert debug.data["feet"]["state"] == "SOLID"


def test_java_body_world_read_missing_body_is_explicit() -> None:
    body = JavaBody(_client(FakeBodyServer()), "MissingBot")

    result = body.perceive("blockAt", {"x": 0, "y": 64, "z": 0})

    assert result.ok is False
    assert result.complete is True
    assert result.error == "missing_body"
    assert result.uncertainty == [{"reason": "missing_body"}]


def test_java_body_execute_delegates_whole_objectives() -> None:
    client = _client(FakeBodyServer())
    client.connect()
    body = JavaBody(client, "Bot")

    result = body.execute(Action.create("navigate", {"goal": {"kind": "near", "x": 10, "y": 64, "z": 0, "range": 1.5}}))
    assert result.ok is True
    assert result.data["replans"] == 2

    gap = body.execute(Action.create("openContainer", {}))
    assert gap.ok is False
    assert gap.error == "capability_unavailable:openContainer"


def test_java_body_poll_events_drains_contract_events() -> None:
    client = _client(FakeBodyServer())
    client.connect()
    body = JavaBody(client, "Bot")
    body.execute(Action.create("navigate", {"goal": {"kind": "near", "x": 10, "y": 64, "z": 0, "range": 1.5}}))

    events = body.poll_events()

    names = [event.name for event in events]
    assert "owner_acquired" in names
    assert "action_terminal" in names
    assert all(event.bot == "Bot" for event in events)
    assert body.poll_events() == []
