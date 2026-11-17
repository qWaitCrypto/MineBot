"""Sans-I/O core for the Baritone-backed real-client Body IPC.

The client initiates a TCP connection, while this module stays independent of
socket and Minecraft APIs. It owns wire framing, authenticated hello, action
idempotency, terminal uniqueness, event sequencing, and state revision rules
shared by the eventual Java/Fabric integration and Python Body adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import secrets
import struct
import time
from typing import Literal, Mapping

from .real_client_support import RealClientTuple


PROTOCOL_NAME = "minebot-body-client"
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
MAX_FRAME_BYTES = 1024 * 1024
_FRAME_PREFIX_BYTES = 4


class RealClientProtocolError(ValueError):
    """The peer violated the real-client Body wire contract."""


class FrameTooLarge(RealClientProtocolError):
    """A frame exceeds the negotiated bounded payload size."""


@dataclass(frozen=True)
class RealClientChallenge:
    """A one-connection challenge; the secret is never serialized."""

    challenge_id: str
    server_nonce: str
    expected_player: str
    expected_endpoint: str | None
    issued_at_ms: int
    max_frame_bytes: int
    _secret: bytes = field(repr=False, compare=False)

    def to_payload(self) -> dict[str, object]:
        return {
            "type": "challenge",
            "protocol": {"name": PROTOCOL_NAME, "major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
            "challenge_id": self.challenge_id,
            "server_nonce": self.server_nonce,
            "max_frame_bytes": self.max_frame_bytes,
        }


@dataclass(frozen=True)
class RealClientIdentity:
    """Identity and exact support tuple reported by a client process."""

    player: str
    session_id: str
    client_epoch: str
    minecraft: str
    baritone: str
    loader_package: str
    java: str
    integration: str
    support_matrix_id: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "RealClientIdentity":
        values = {
            "player": payload.get("player"),
            "session_id": payload.get("session_id"),
            "client_epoch": payload.get("client_epoch"),
            "minecraft": payload.get("minecraft_version"),
            "baritone": payload.get("baritone_version"),
            "loader_package": payload.get("loader_package"),
            "java": payload.get("java_runtime"),
            "integration": payload.get("integration_version"),
            "support_matrix_id": payload.get("support_matrix_id"),
        }
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise RealClientProtocolError("hello is missing a non-empty client identity field")
        return cls(**values)  # type: ignore[arg-type]

    def tuple(self) -> RealClientTuple:
        return RealClientTuple(
            minecraft=self.minecraft,
            baritone=self.baritone,
            loader_package=self.loader_package,
            java=self.java,
            integration=self.integration,
        )


def issue_challenge(
    expected_player: str,
    *,
    expected_endpoint: str | None = None,
    secret: bytes | None = None,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> RealClientChallenge:
    """Create a fresh challenge and per-process secret for one connection."""

    if not expected_player.strip():
        raise ValueError("expected_player must be non-empty")
    if not 1 <= max_frame_bytes <= MAX_FRAME_BYTES:
        raise ValueError("max_frame_bytes must be between 1 and 1 MiB")
    key = secrets.token_bytes(32) if secret is None else bytes(secret)
    if len(key) < 16:
        raise ValueError("challenge secret must contain at least 128 bits")
    return RealClientChallenge(
        challenge_id=secrets.token_hex(16),
        server_nonce=secrets.token_urlsafe(24),
        expected_player=expected_player,
        expected_endpoint=expected_endpoint,
        issued_at_ms=int(time.time() * 1000),
        max_frame_bytes=max_frame_bytes,
        _secret=key,
    )


def _auth_material(
    *,
    server_nonce: str,
    client_nonce: str,
    session_id: str,
    player: str,
    endpoint: str | None,
) -> bytes:
    body = {
        "protocol": {"name": PROTOCOL_NAME, "major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
        "server_nonce": server_nonce,
        "client_nonce": client_nonce,
        "session_id": session_id,
        "player": player,
        "endpoint": endpoint,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def build_hello(
    challenge: RealClientChallenge,
    identity: RealClientIdentity,
    *,
    client_nonce: str | None = None,
    capabilities: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the client hello and bind identity to the challenge."""

    nonce = client_nonce or secrets.token_urlsafe(24)
    if not nonce or nonce == challenge.server_nonce:
        raise ValueError("client_nonce must be fresh and different from server_nonce")
    digest = hmac.new(
        challenge._secret,
        _auth_material(
            server_nonce=challenge.server_nonce,
            client_nonce=nonce,
            session_id=identity.session_id,
            player=identity.player,
            endpoint=challenge.expected_endpoint,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        "type": "hello",
        "protocol": {"name": PROTOCOL_NAME, "major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
        "session_id": identity.session_id,
        "client_epoch": identity.client_epoch,
        "client_nonce": nonce,
        "challenge_id": challenge.challenge_id,
        "challenge_hmac": digest,
        "player": identity.player,
        "minecraft_version": identity.minecraft,
        "baritone_version": identity.baritone,
        "integration_version": identity.integration,
        "loader_package": identity.loader_package,
        "java_runtime": identity.java,
        "support_matrix_id": identity.support_matrix_id,
        "capabilities": dict(capabilities or {}),
    }


def verify_hello(
    challenge: RealClientChallenge,
    hello: Mapping[str, object],
    *,
    expected_identity: RealClientIdentity | None = None,
) -> RealClientIdentity:
    """Authenticate and validate one hello before accepting its identity."""

    if hello.get("type") != "hello":
        raise RealClientProtocolError("expected hello")
    protocol = hello.get("protocol")
    if not isinstance(protocol, Mapping) or protocol.get("name") != PROTOCOL_NAME:
        raise RealClientProtocolError("unsupported real-client protocol")
    if protocol.get("major") != PROTOCOL_MAJOR:
        raise RealClientProtocolError("real-client protocol major mismatch")
    if not isinstance(protocol.get("minor"), int) or protocol["minor"] > PROTOCOL_MINOR:
        raise RealClientProtocolError("real-client protocol minor is newer than this adapter")
    if hello.get("challenge_id") != challenge.challenge_id:
        raise RealClientProtocolError("challenge_id mismatch")
    client_nonce = hello.get("client_nonce")
    if not isinstance(client_nonce, str) or not client_nonce or client_nonce == challenge.server_nonce:
        raise RealClientProtocolError("invalid client_nonce")
    player = hello.get("player")
    session_id = hello.get("session_id")
    provided_hmac = hello.get("challenge_hmac")
    if not all(isinstance(value, str) and value for value in (player, session_id, provided_hmac)):
        raise RealClientProtocolError("hello is missing authentication fields")
    expected_hmac = hmac.new(
        challenge._secret,
        _auth_material(
            server_nonce=challenge.server_nonce,
            client_nonce=client_nonce,
            session_id=session_id,
            player=player,
            endpoint=challenge.expected_endpoint,
        ),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided_hmac, expected_hmac):
        raise RealClientProtocolError("challenge_hmac mismatch")
    if player != challenge.expected_player:
        raise RealClientProtocolError("unexpected player identity")
    identity = RealClientIdentity.from_payload(hello)
    if expected_identity is not None and identity != expected_identity:
        raise RealClientProtocolError("client identity does not match the selected tuple")
    return identity


def build_ready(challenge: RealClientChallenge, identity: RealClientIdentity) -> dict[str, object]:
    """Build the server's post-authentication readiness envelope."""

    return {
        "type": "ready",
        "protocol": {"name": PROTOCOL_NAME, "major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
        "challenge_id": challenge.challenge_id,
        "session_id": identity.session_id,
        "client_epoch": identity.client_epoch,
        "max_frame_bytes": challenge.max_frame_bytes,
    }


class FrameCodec:
    """Length-prefixed UTF-8 JSON codec with bounded payloads."""

    def __init__(self, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        if not 1 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 1 and 1 MiB")
        self.max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()

    def encode(self, message: Mapping[str, object]) -> bytes:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(raw) > self.max_frame_bytes:
            raise FrameTooLarge(f"frame payload is {len(raw)} bytes; max is {self.max_frame_bytes}")
        return struct.pack(">I", len(raw)) + raw

    def feed(self, data: bytes) -> list[dict[str, object]]:
        self._buffer.extend(data)
        messages: list[dict[str, object]] = []
        while True:
            if len(self._buffer) < _FRAME_PREFIX_BYTES:
                break
            size = struct.unpack(">I", self._buffer[:_FRAME_PREFIX_BYTES])[0]
            if size == 0 or size > self.max_frame_bytes:
                raise FrameTooLarge(f"frame payload is {size} bytes; max is {self.max_frame_bytes}")
            if len(self._buffer) < _FRAME_PREFIX_BYTES + size:
                break
            raw = bytes(self._buffer[_FRAME_PREFIX_BYTES : _FRAME_PREFIX_BYTES + size])
            del self._buffer[: _FRAME_PREFIX_BYTES + size]
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RealClientProtocolError("frame payload is not UTF-8 JSON") from error
            if not isinstance(decoded, dict) or not isinstance(decoded.get("type"), str):
                raise RealClientProtocolError("frame payload must be a JSON object with type")
            messages.append(decoded)
        return messages

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)


@dataclass(frozen=True)
class CommandDecision:
    disposition: Literal["accepted", "replay", "conflict"]
    action_id: str
    action_generation: int
    recorded: dict[str, object] | None = None


class ActionLedger:
    """Enforce one command identity and one terminal per action."""

    def __init__(self) -> None:
        self._commands: dict[str, tuple[int, str, dict[str, object]]] = {}
        self._terminals: dict[tuple[str, int], dict[str, object]] = {}

    def register_command(self, command: Mapping[str, object]) -> CommandDecision:
        if command.get("type") != "command":
            raise RealClientProtocolError("expected command")
        action_id = command.get("action_id")
        generation = command.get("action_generation")
        if not isinstance(action_id, str) or not action_id:
            raise RealClientProtocolError("command requires action_id")
        if not isinstance(generation, int) or generation < 0:
            raise RealClientProtocolError("command requires non-negative action_generation")
        material = dict(command)
        material.pop("deadline", None)
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        previous = self._commands.get(action_id)
        if previous is None:
            self._commands[action_id] = (generation, digest, dict(command))
            return CommandDecision("accepted", action_id, generation)
        old_generation, old_digest, old_command = previous
        if old_generation == generation and hmac.compare_digest(old_digest, digest):
            return CommandDecision("replay", action_id, generation, dict(old_command))
        return CommandDecision("conflict", action_id, generation, dict(old_command))

    def record_terminal(self, event: Mapping[str, object]) -> bool:
        """Record a terminal; return False for an identical replay."""

        if event.get("type") != "event" or event.get("event_kind") != "action_terminal":
            raise RealClientProtocolError("expected action_terminal event")
        action_id = event.get("action_id")
        generation = event.get("action_generation")
        if not isinstance(action_id, str) or not action_id:
            raise RealClientProtocolError("terminal requires action_id")
        if not isinstance(generation, int) or generation < 0:
            raise RealClientProtocolError("terminal requires non-negative action_generation")
        if not isinstance(event.get("success"), bool) or not isinstance(event.get("reason"), str):
            raise RealClientProtocolError("terminal requires success and reason")
        key = (action_id, generation)
        command = self._commands.get(action_id)
        if command is None or command[0] != generation:
            raise RealClientProtocolError("terminal does not match a registered command")
        previous = self._terminals.get(key)
        normalized = dict(event)
        if previous is None:
            self._terminals[key] = normalized
            return True
        if previous == normalized:
            return False
        raise RealClientProtocolError("conflicting terminal for the same action")

    def terminal(self, action_id: str, action_generation: int) -> dict[str, object] | None:
        value = self._terminals.get((action_id, action_generation))
        return None if value is None else dict(value)


@dataclass(frozen=True)
class EventObservation:
    accepted: bool
    gap: tuple[int, int] | None = None


class EventLedger:
    """Track critical event sequence and coalescible state revisions."""

    def __init__(self, *, client_epoch: str | None = None) -> None:
        if client_epoch is not None and not client_epoch:
            raise ValueError("client_epoch must be non-empty when supplied")
        self.client_epoch = client_epoch
        self._last_event_seq = 0
        self._last_state_revision = 0
        self.gaps: list[tuple[int, int]] = []

    def observe_event(self, event: Mapping[str, object]) -> EventObservation:
        if event.get("type") != "event":
            raise RealClientProtocolError("expected event")
        self._check_epoch(event)
        sequence = event.get("event_seq")
        if not isinstance(sequence, int) or sequence <= 0:
            raise RealClientProtocolError("event requires positive event_seq")
        if sequence <= self._last_event_seq:
            return EventObservation(False)
        gap = None
        if sequence > self._last_event_seq + 1:
            gap = (self._last_event_seq + 1, sequence - 1)
            self.gaps.append(gap)
        self._last_event_seq = sequence
        return EventObservation(True, gap)

    def observe_state(self, snapshot: Mapping[str, object]) -> bool:
        if snapshot.get("type") != "state_snapshot":
            raise RealClientProtocolError("expected state_snapshot")
        self._check_epoch(snapshot)
        revision = snapshot.get("state_revision")
        if not isinstance(revision, int) or revision <= 0:
            raise RealClientProtocolError("state_snapshot requires positive state_revision")
        if revision <= self._last_state_revision:
            return False
        self._last_state_revision = revision
        return True

    def _check_epoch(self, message: Mapping[str, object]) -> None:
        epoch = message.get("client_epoch")
        if not isinstance(epoch, str) or not epoch:
            raise RealClientProtocolError("message requires client_epoch")
        if self.client_epoch is None:
            self.client_epoch = epoch
        elif epoch != self.client_epoch:
            raise RealClientProtocolError("client_epoch changed within one connection")

    @property
    def last_event_seq(self) -> int:
        return self._last_event_seq

    @property
    def last_state_revision(self) -> int:
        return self._last_state_revision
