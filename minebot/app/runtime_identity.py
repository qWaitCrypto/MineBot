"""Server-authoritative runtime identity resolution."""

from __future__ import annotations

import json
import re
from typing import Protocol
from uuid import uuid4

from minebot.app.runtime_state import RuntimeScope


WORLD_ID_STORAGE = "minebot:runtime"
WORLD_ID_PATH = "world_id"
_WORLD_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class RuntimeIdentityError(RuntimeError):
    """The server could not provide a valid world identity."""


class CommandTransport(Protocol):
    def request(self, command: str) -> str: ...


def resolve_runtime_scope(
    transport: CommandTransport,
    *,
    server_id: str,
    bot_id: str,
    world_id_override: str | None = None,
) -> RuntimeScope:
    world_id = world_id_override or ensure_world_identity(transport)
    return RuntimeScope(server_id=server_id, world_id=world_id, bot_id=bot_id)


def ensure_world_identity(transport: CommandTransport) -> str:
    """Read or atomically initialize an identity stored with the world save."""
    candidate = f"world-{uuid4().hex}"
    transport.request(
        "execute unless data storage "
        f"{WORLD_ID_STORAGE} {WORLD_ID_PATH} run data modify storage "
        f'{WORLD_ID_STORAGE} {WORLD_ID_PATH} set value "{candidate}"'
    )
    response = transport.request(
        f"data get storage {WORLD_ID_STORAGE} {WORLD_ID_PATH}"
    )
    world_id = parse_world_identity_response(response)
    if world_id is None:
        raise RuntimeIdentityError(
            "world identity marker was not readable after initialization"
        )
    return world_id


def parse_world_identity_response(response: str) -> str | None:
    prefix = "has the following contents:"
    if prefix not in response:
        return None
    raw = response.split(prefix, 1)[1].strip()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, str) or _WORLD_ID_PATTERN.fullmatch(value) is None:
        return None
    return value


__all__ = [
    "RuntimeIdentityError",
    "ensure_world_identity",
    "parse_world_identity_response",
    "resolve_runtime_scope",
]
