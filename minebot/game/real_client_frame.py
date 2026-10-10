"""The client's state frame, and telling it apart from what an action did.

The game client answers every action with its whole state frame (entities,
every inventory slot, pathfinder settings, heap sizes) and only then adds what
the action did. That frame is ambient state: the Agent gets the current one
with each request and has read tools for the rest, so a copy inside every
action result, and inside every step of a composite result, is noise.

Which fields form the frame is the client's business, so it is learned from
the client's own ``state`` replies instead of being listed here. Results are
stored whole; this only shapes what is shown.
"""

from __future__ import annotations

CLIENT_PROTOCOL_PREFIX = "minebot-client"
_FRAME_ENVELOPE_FIELDS = frozenset({"type", "request_id", "protocol"})
# Where the Body ended up is an outcome of the action, not ambient state.
ACTION_OUTCOME_STATE_FIELDS = frozenset({"position"})
# Kept when the action did not complete: they say where the path stopped.
ACTION_FAILURE_STATE_FIELDS = frozenset({"navigation"})

_client_state_fields: frozenset[str] = frozenset()


def learn_client_state_fields(frame: object) -> None:
    """Remember which fields the client's state frame consists of."""
    global _client_state_fields
    if not isinstance(frame, dict) or frame.get("type") != "state":
        return
    _client_state_fields = _client_state_fields | (frozenset(str(key) for key in frame) - _FRAME_ENVELOPE_FIELDS)


def client_state_fields() -> frozenset[str]:
    return _client_state_fields


def reset_client_state_fields() -> None:
    global _client_state_fields
    _client_state_fields = frozenset()


def is_client_action_frame(value: object) -> bool:
    """A client reply to an action: the state frame plus what the action did."""
    if not isinstance(value, dict):
        return False
    protocol = value.get("protocol")
    kind = value.get("type")
    return (isinstance(protocol, str) and protocol.startswith(CLIENT_PROTOCOL_PREFIX)
            and isinstance(kind, str) and kind != "state")


def without_ambient_state(value: object) -> object:
    """Copy of ``value`` with the state frame removed from every action reply in it.

    Replies to a ``state`` request and anything that is not a client reply are
    left as they are: there the state is the answer.
    """
    fields = _client_state_fields
    if not fields:
        return value
    return _without_ambient_state(value, fields)


def _without_ambient_state(value: object, fields: frozenset[str]) -> object:
    if isinstance(value, dict):
        if is_client_action_frame(value):
            keep = ACTION_OUTCOME_STATE_FIELDS
            if value.get("status") not in {None, "completed"} or value.get("success") is False:
                keep = keep | ACTION_FAILURE_STATE_FIELDS
            return {
                key: _without_ambient_state(item, fields)
                for key, item in value.items()
                if (key not in fields and key not in _FRAME_ENVELOPE_FIELDS) or key in keep
            }
        return {key: _without_ambient_state(item, fields) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_ambient_state(item, fields) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_ambient_state(item, fields) for item in value)
    return value


__all__ = [
    "client_state_fields",
    "is_client_action_frame",
    "learn_client_state_fields",
    "reset_client_state_fields",
    "without_ambient_state",
]
