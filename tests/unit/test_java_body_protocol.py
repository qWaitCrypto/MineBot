"""Fixture-driven conformance tests for the fakeplayer-body/1 protocol core.

The golden transcripts in tests/protocol_fixtures/ are the cross-language
contract: this driver proves the Python sans-io core against them, and the
Java side consumes the same files. A changed transcript is a contract change,
not a test fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minebot.game.java_body_protocol import (
    BotEvent,
    CapabilityGap,
    ErrorResponse,
    EventGap,
    JavaBodyProtocol,
    MAX_FIND_BLOCK_IDS,
    MAX_TERMINAL_BYTES,
    ProtocolViolation,
    Response,
    ServerProposal,
)


FIXTURES_DIR = Path("tests/protocol_fixtures")
FIXTURES = sorted(FIXTURES_DIR.glob("*.json"))


def _assert_subset(expected: dict, actual: dict, context: str) -> None:
    for key, value in expected.items():
        assert key in actual, f"{context}: missing {key!r}"
        assert actual[key] == value, f"{context}: {key!r} is {actual[key]!r}, expected {value!r}"


def _assert_item(expected: dict, item: object, context: str) -> None:
    kind = expected["kind"]
    if kind == "response":
        assert isinstance(item, Response), f"{context}: expected Response, got {type(item).__name__}"
        assert item.type == expected["type"], context
        if "request_type" in expected:
            assert item.request_type == expected["request_type"], context
        _assert_subset(expected.get("payload_contains", {}), item.payload, context)
    elif kind == "error":
        assert isinstance(item, ErrorResponse), f"{context}: expected ErrorResponse, got {type(item).__name__}"
        assert item.code == expected["code"], context
        if "retryable" in expected:
            assert item.retryable == expected["retryable"], context
        if "request_type" in expected:
            assert item.request_type == expected["request_type"], context
        _assert_subset(expected.get("payload_contains", {}), item.payload, context)
    elif kind == "event":
        assert isinstance(item, BotEvent), f"{context}: expected BotEvent, got {type(item).__name__}"
        assert item.name == expected["name"], context
        if "seq" in expected:
            assert item.seq == expected["seq"], context
        if "action_id" in expected:
            assert item.action_id == expected["action_id"], context
        _assert_subset(expected.get("data_contains", {}), item.data, context)
    elif kind == "gap":
        assert isinstance(item, EventGap), f"{context}: expected EventGap, got {type(item).__name__}"
        assert item.from_seq == expected["from_seq"], context
        assert item.to_seq == expected["to_seq"], context
    elif kind == "proposal":
        assert isinstance(item, ServerProposal), f"{context}: expected ServerProposal, got {type(item).__name__}"
        assert item.proposal_id == expected["proposal_id"], context
        assert item.block_id == expected["block_id"], context
        assert item.kind == expected["mutation_kind"], context
        assert (item.x, item.y, item.z) == (expected["x"], expected["y"], expected["z"]), context
        if "context" in expected:
            assert item.context == expected["context"], context
    else:
        raise AssertionError(f"{context}: unknown expectation kind {kind!r}")


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.stem)
def test_fixture_transcript(fixture_path: Path) -> None:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    protocol = JavaBodyProtocol()

    for step_index, step in enumerate(fixture["steps"]):
        context = f"{fixture['name']}#{step_index}"
        if "send" in step:
            builder = getattr(protocol, step["send"]["builder"])
            built = builder(**step["send"]["args"])
            assert built == step["expect_message"], f"{context}: built request diverges from the golden transcript"
        else:
            items = protocol.feed(step["recv"])
            expected_items = step["expect"]
            assert len(items) == len(expected_items), (
                f"{context}: got {len(items)} items {[type(i).__name__ for i in items]}, expected {len(expected_items)}"
            )
            for expected, item in zip(expected_items, items):
                _assert_item(expected, item, context)

    final = fixture.get("final", {})
    if "negotiated" in final:
        assert protocol.negotiated == final["negotiated"]
    for request_type in final.get("supports", []):
        assert protocol.supports(request_type), request_type
    for request_type in final.get("not_supported", []):
        assert not protocol.supports(request_type), request_type
    if "pending_requests" in final:
        assert len(protocol.pending_request_ids()) == final["pending_requests"]
    for bot, seq in final.get("last_seq", {}).items():
        assert protocol.last_seq(bot) == seq


def test_fixture_directory_is_not_empty() -> None:
    assert FIXTURES, "the protocol fixture suite must exist"


def test_bounded_truncated_terminal_is_preserved_from_event() -> None:
    protocol = JavaBodyProtocol()
    terminal = {
        "classification": "failed",
        "reason": "no_path",
        "reason_class": "blocked",
        "goal": {"action_id": "a-1", "objective": "NAVIGATE", "bot": "Bot"},
        "attempts_consumed": 3,
        "budget_remaining": 1,
        "closest_approach_distance": 7.5,
        "unloaded_chunk_count": 0,
        "elapsed_ticks": 40,
        "evidence_truncated": True,
        "evidence_omitted_fields": ["planning_attempts"],
    }

    items = protocol.feed(
        {
            "channel": "fakeplayer-body",
            "type": "EVENT",
            "bot": "Bot",
            "seq": 1,
            "tick": 50,
            "event": "action_terminal",
            "action_id": "a-1",
            "data": terminal,
        }
    )

    assert len(items) == 1
    assert isinstance(items[0], BotEvent)
    assert items[0].data == terminal


def test_oversized_terminal_is_a_protocol_violation_not_reconciliation_unknown() -> None:
    protocol = JavaBodyProtocol()
    with pytest.raises(ProtocolViolation, match="terminal exceeds"):
        protocol.feed(
            {
                "channel": "fakeplayer-body",
                "type": "EVENT",
                "bot": "Bot",
                "seq": 1,
                "tick": 50,
                "event": "action_terminal",
                "action_id": "a-large",
                "data": {
                    "classification": "failed",
                    "reason": "no_path",
                    "diagnostics": "x" * MAX_TERMINAL_BYTES,
                },
            }
        )


def test_requests_before_negotiation_are_a_capability_gap() -> None:
    protocol = JavaBodyProtocol()
    with pytest.raises(CapabilityGap):
        protocol.find_blocks("MineBot_1", ["minecraft:oak_log"], 32)
    with pytest.raises(CapabilityGap):
        protocol.navigate("MineBot_1", "nav-1", {"kind": "xz", "x": 1, "z": 2})


def test_hello_can_disable_live_event_subscription_without_changing_legacy_default() -> None:
    protocol = JavaBodyProtocol()

    legacy = protocol.hello()
    read_only = protocol.hello(subscribe_events=False)

    assert "subscribe_events" not in legacy
    assert read_only["subscribe_events"] is False


def test_unoffered_request_type_is_a_capability_gap_never_a_silent_substitute() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    protocol.feed(
        {
            "channel": "fakeplayer-body",
            "type": "HELLO_ACK",
            "request_id": hello["request_id"],
            "protocol": "fakeplayer-body/1",
            "minecraft_version": "26.1.2",
            "max_request_bytes": 16384,
            "max_requests_per_second": 40,
            "request_types": ["FIND_BLOCKS", "HELLO"],
        }
    )
    with pytest.raises(CapabilityGap):
        protocol.navigate("MineBot_1", "nav-1", {"kind": "xz", "x": 1, "z": 2})
    assert protocol.supports("FIND_BLOCKS")


def test_find_blocks_carries_one_canonical_multi_target_query() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    protocol.feed(
        {
            "channel": "fakeplayer-body",
            "type": "HELLO_ACK",
            "request_id": hello["request_id"],
            "protocol": "fakeplayer-body/1",
            "minecraft_version": "26.1.2",
            "max_request_bytes": 16384,
            "max_requests_per_second": 40,
            "request_types": ["FIND_BLOCKS", "HELLO"],
        }
    )
    block_ids = [f"minecraft:test_block_{index}" for index in range(32)]

    request = protocol.find_blocks("MineBot_1", block_ids, 24)

    assert request["block_ids"] == block_ids
    assert MAX_FIND_BLOCK_IDS == 64
    with pytest.raises(ValueError, match="1 to 64"):
        protocol.find_blocks(
            "MineBot_1",
            [f"minecraft:test_block_{index}" for index in range(65)],
            24,
        )


def test_survival_owner_and_recovery_navigation_are_explicit_protocol_facts() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    protocol.feed({
        "channel": "fakeplayer-body",
        "type": "HELLO_ACK",
        "request_id": hello["request_id"],
        "protocol": "fakeplayer-body/1",
        "minecraft_version": "26.1.2",
        "max_request_bytes": 16384,
        "max_requests_per_second": 40,
        "request_types": ["HELLO", "NAVIGATE", "SET_SURVIVAL_OWNER"],
    })

    ownership = protocol.set_survival_owner("MineBot_1", True)
    navigation = protocol.navigate(
        "MineBot_1",
        "nav-recovery-1",
        {"kind": "near", "x": 4, "y": 64, "z": 0, "range": 0.5},
        survival_recovery=True,
    )

    assert ownership["enabled"] is True
    assert navigation["survival_recovery"] is True


def test_navigation_carries_finite_body_profile_without_strategy_fields() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    protocol.feed({
        "channel": "fakeplayer-body",
        "type": "HELLO_ACK",
        "request_id": hello["request_id"],
        "protocol": "fakeplayer-body/1",
        "minecraft_version": "26.1.2",
        "max_request_bytes": 16384,
        "max_requests_per_second": 40,
        "request_types": ["HELLO", "NAVIGATE"],
    })

    navigation = protocol.navigate(
        "MineBot_1",
        "nav-profile-1",
        {"kind": "xz", "x": 8, "z": 0},
        grid_radius=48,
        max_expand=1200,
        allow_diagonal=False,
        allow_ascend=True,
        allow_descend=False,
        allow_swim=False,
        max_fall_depth=2,
        max_water_drop_depth=8,
    )

    assert navigation["grid_radius"] == 48
    assert navigation["max_expand"] == 1200
    assert navigation["allow_diagonal"] is False
    assert navigation["allow_swim"] is False
    assert navigation["max_water_drop_depth"] == 8
    assert "blocks" not in navigation
    assert "target_count" not in navigation


def test_navigation_governed_break_contract_is_explicit_and_bounded() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    protocol.feed({
        "channel": "fakeplayer-body",
        "type": "HELLO_ACK",
        "request_id": hello["request_id"],
        "protocol": "fakeplayer-body/1",
        "minecraft_version": "26.1.2",
        "max_request_bytes": 16384,
        "max_requests_per_second": 40,
        "request_types": ["HELLO", "NAVIGATE"],
    })

    navigation = protocol.navigate(
        "MineBot_1",
        "nav-governed-1",
        {"kind": "xz", "x": 8, "z": 0},
        allow_break=True,
        break_budget=3,
        break_context="travel",
        denied_mutations=[[1, 64, 0]],
    )

    assert navigation["allow_break"] is True
    assert navigation["break_budget"] == 3
    assert navigation["break_context"] == "travel"
    assert navigation["denied_mutations"] == [[1, 64, 0]]
    assert "blocks" not in navigation
    assert "radius" not in navigation


def test_navigation_rejects_implicit_or_unbounded_break_authority() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    protocol.feed({
        "channel": "fakeplayer-body",
        "type": "HELLO_ACK",
        "request_id": hello["request_id"],
        "protocol": "fakeplayer-body/1",
        "minecraft_version": "26.1.2",
        "max_request_bytes": 16384,
        "max_requests_per_second": 40,
        "request_types": ["HELLO", "NAVIGATE"],
    })

    with pytest.raises(ValueError, match="positive break_budget"):
        protocol.navigate(
            "MineBot_1",
            "nav-governed-2",
            {"kind": "xz", "x": 8, "z": 0},
            allow_break=True,
            break_context="travel",
        )


def test_protocol_version_mismatch_fails_closed() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    with pytest.raises(ProtocolViolation):
        protocol.feed(
            {
                "channel": "fakeplayer-body",
                "type": "HELLO_ACK",
                "request_id": hello["request_id"],
                "protocol": "fakeplayer-body/2",
                "request_types": ["HELLO"],
            }
        )


def test_unknown_request_id_and_wrong_channel_are_violations() -> None:
    protocol = JavaBodyProtocol()
    with pytest.raises(ProtocolViolation):
        protocol.feed({"channel": "fakeplayer-body", "type": "HELLO_ACK", "request_id": "r-99"})
    with pytest.raises(ProtocolViolation):
        protocol.feed({"channel": "observer-control", "type": "EVENT"})


def test_malformed_proposal_is_a_violation_never_a_guess() -> None:
    protocol = JavaBodyProtocol()
    with pytest.raises(ProtocolViolation):
        protocol.feed(
            {
                "channel": "fakeplayer-body",
                "type": "MUTATION_PROPOSAL",
                "proposal_id": "mp-9",
                "bot": "B",
                "mutation": {"kind": "break", "block_id": "minecraft:oak_log"},
            }
        )


def test_mismatched_response_type_is_a_violation() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    with pytest.raises(ProtocolViolation):
        protocol.feed(
            {
                "channel": "fakeplayer-body",
                "type": "FIND_BLOCKS_RESULT",
                "request_id": hello["request_id"],
            }
        )


def test_container_transfer_params_cannot_override_caller_identity() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    protocol.feed({
        "channel": "fakeplayer-body",
        "type": "HELLO_ACK",
        "request_id": hello["request_id"],
        "protocol": "fakeplayer-body/1",
        "minecraft_version": "26.1.2",
        "max_request_bytes": 16384,
        "max_requests_per_second": 40,
        "request_types": ["HELLO", "CONTAINER_TRANSFER"],
    })

    request = protocol.container_transfer(
        "MineBot_1",
        "container-1",
        {
            "bot_name": "OtherBot",
            "action_id": "other-action",
            "pos": [1, 64, 0],
            "direction": "container_to_bot",
            "container_slot": 0,
            "bot_slot": 1,
        },
    )

    assert request["bot_name"] == "MineBot_1"
    assert request["action_id"] == "container-1"


def test_furnace_transfer_params_cannot_override_caller_identity() -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    protocol.feed({
        "channel": "fakeplayer-body",
        "type": "HELLO_ACK",
        "request_id": hello["request_id"],
        "protocol": "fakeplayer-body/1",
        "minecraft_version": "26.1.2",
        "max_request_bytes": 16384,
        "max_requests_per_second": 40,
        "request_types": ["HELLO", "FURNACE_TRANSFER"],
    })

    request = protocol.furnace_transfer(
        "MineBot_1",
        "furnace-1",
        {
            "bot_name": "OtherBot",
            "action_id": "other-action",
            "pos": [1, 64, 0],
            "direction": "bot_to_furnace",
            "furnace_slot": "input",
            "bot_slot": 5,
        },
    )

    assert request["bot_name"] == "MineBot_1"
    assert request["action_id"] == "furnace-1"


@pytest.mark.parametrize("builder_name", ["engage_entity", "follow_entity"])
def test_entity_actions_require_agent_resolved_canonical_uuid(builder_name: str) -> None:
    protocol = JavaBodyProtocol()
    hello = protocol.hello()
    request_type = "ENGAGE_ENTITY" if builder_name == "engage_entity" else "FOLLOW_ENTITY"
    protocol.feed({
        "channel": "fakeplayer-body",
        "type": "HELLO_ACK",
        "request_id": hello["request_id"],
        "protocol": "fakeplayer-body/1",
        "minecraft_version": "26.1.2",
        "max_request_bytes": 16384,
        "max_requests_per_second": 40,
        "request_types": ["HELLO", request_type],
    })
    builder = getattr(protocol, builder_name)

    with pytest.raises(ValueError, match="target_spec"):
        builder("MineBot_1", "action-1", {"target_spec": "nearest_hostile"})
    with pytest.raises(ValueError, match="canonical"):
        builder("MineBot_1", "action-2", {"target_uuid": "not-a-uuid"})

    request = builder(
        "MineBot_1",
        "action-3",
        {"target_uuid": "11111111-1111-4111-8111-111111111111"},
    )
    assert request["target_uuid"] == "11111111-1111-4111-8111-111111111111"
