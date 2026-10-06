from __future__ import annotations

import struct

import pytest

from minebot.game.real_client_protocol import (
    ActionLedger,
    EventLedger,
    FrameCodec,
    FrameTooLarge,
    RealClientIdentity,
    RealClientProtocolError,
    build_hello,
    build_ready,
    issue_challenge,
    verify_hello,
)
from minebot.game.real_client_support import (
    CANDIDATE_26_1_2,
    RealClientTuple,
    UnsupportedRealClientTuple,
    require_supported_real_client_tuple,
)


def identity() -> RealClientIdentity:
    return RealClientIdentity(
        player="Bot1",
        session_id="session-1",
        client_epoch="epoch-1",
        minecraft="26.1.2",
        baritone="v1.18.0",
        loader_package="fabric-0.19.3",
        java="25",
        integration="minebot-client-spike",
        support_matrix_id=CANDIDATE_26_1_2.matrix_id,
    )


def test_frame_codec_handles_fragmented_frames_and_multiple_messages() -> None:
    codec = FrameCodec()
    first = codec.encode({"type": "heartbeat", "n": 1})
    second = codec.encode({"type": "state_snapshot", "state_revision": 2})

    assert codec.feed(first[:2]) == []
    assert codec.feed(first[2:] + second[:5]) == [{"type": "heartbeat", "n": 1}]
    assert codec.feed(second[5:]) == [{"type": "state_snapshot", "state_revision": 2}]
    assert codec.pending_bytes == 0


def test_frame_codec_rejects_oversize_payload_and_wire_length() -> None:
    codec = FrameCodec(max_frame_bytes=32)
    with pytest.raises(FrameTooLarge):
        codec.encode({"type": "event", "payload": "x" * 64})
    with pytest.raises(FrameTooLarge):
        codec.feed(struct.pack(">I", 33))


def test_handshake_binds_identity_to_fresh_challenge() -> None:
    challenge = issue_challenge("Bot1", expected_endpoint="127.0.0.1:43111", secret=b"s" * 32)
    hello = build_hello(challenge, identity(), client_nonce="client-nonce")

    accepted = verify_hello(challenge, hello, expected_identity=identity())
    assert accepted == identity()
    ready = build_ready(challenge, accepted)
    assert ready["type"] == "ready"
    assert ready["client_epoch"] == "epoch-1"

    tampered = dict(hello)
    tampered["player"] = "OtherBot"
    with pytest.raises(RealClientProtocolError):
        verify_hello(challenge, tampered)


def test_action_ledger_replays_duplicate_commands_and_rejects_conflicts() -> None:
    ledger = ActionLedger()
    command = {
        "type": "command",
        "action_id": "a1",
        "action_generation": 3,
        "deadline": 100,
        "objective": {"kind": "navigate", "x": 4, "y": 64, "z": 9},
    }
    assert ledger.register_command(command).disposition == "accepted"
    retry = dict(command, deadline=200)
    assert ledger.register_command(retry).disposition == "replay"
    assert ledger.register_command(dict(command, objective={"kind": "mine"})).disposition == "conflict"

    terminal = {
        "type": "event",
        "event_kind": "action_terminal",
        "event_seq": 1,
        "client_epoch": "epoch-1",
        "action_id": "a1",
        "action_generation": 3,
        "success": True,
        "reason": "arrived",
    }
    assert ledger.record_terminal(terminal) is True
    assert ledger.record_terminal(terminal) is False
    with pytest.raises(RealClientProtocolError):
        ledger.record_terminal(dict(terminal, reason="timeout"))


def test_event_ledger_records_gap_and_coalesces_old_state() -> None:
    ledger = EventLedger(client_epoch="epoch-1")
    assert ledger.observe_event({"type": "event", "client_epoch": "epoch-1", "event_seq": 1}).accepted
    observation = ledger.observe_event({"type": "event", "client_epoch": "epoch-1", "event_seq": 4})
    assert observation.gap == (2, 3)
    assert ledger.observe_event({"type": "event", "client_epoch": "epoch-1", "event_seq": 3}).accepted is False
    assert ledger.observe_state({"type": "state_snapshot", "client_epoch": "epoch-1", "state_revision": 2})
    assert ledger.observe_state({"type": "state_snapshot", "client_epoch": "epoch-1", "state_revision": 1}) is False
    with pytest.raises(RealClientProtocolError):
        ledger.observe_event({"type": "event", "client_epoch": "epoch-2", "event_seq": 5})


def test_support_matrix_does_not_promote_candidate_before_live_proof() -> None:
    with pytest.raises(UnsupportedRealClientTuple):
        require_supported_real_client_tuple(CANDIDATE_26_1_2.tuple)
    with pytest.raises(UnsupportedRealClientTuple):
        require_supported_real_client_tuple(
            RealClientTuple("26.1.2", "v1.18.0", "fabric-0.19.3", "25", "other-integration")
        )
