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


def test_requests_before_negotiation_are_a_capability_gap() -> None:
    protocol = JavaBodyProtocol()
    with pytest.raises(CapabilityGap):
        protocol.find_blocks("MineBot_1", ["minecraft:oak_log"], 32)
    with pytest.raises(CapabilityGap):
        protocol.navigate("MineBot_1", "nav-1", {"kind": "xz", "x": 1, "z": 2})


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
