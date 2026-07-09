"""Pure protocol helpers for the Scarpet Body app."""

from __future__ import annotations

import json
import re
from typing import Any

from minebot.game.errors import EnvelopeError, IncompletePayloadError, TruncatedPayloadError
from minebot.contract import Action, BodyState, Event, PerceptionResult, Result

RCON_TRUNCATION_LIMIT = 4096
SCARPET_APP = "minebot"
_SCRIPT_PREFIX_RE = re.compile(r"^\s*=\s*")
_SCARPET_TIMING_TOKEN_RE = re.compile(r"\(\d+(?:\.\d+)?[a-zµ]+\)")


def build_action_call(bot: str, action: Action, app: str = SCARPET_APP) -> str:
    return _script_call(app, "minebot_action", bot, action.to_payload())


def build_state_call(bot: str, app: str = SCARPET_APP) -> str:
    return _script_call(app, "minebot_state", bot)


def build_perceive_call(bot: str, scope: str, params: dict[str, Any], app: str = SCARPET_APP) -> str:
    return _script_call(app, "minebot_perceive", bot, scope, params)


def build_drain_call(bot: str, app: str = SCARPET_APP, *, since_seq: int | None = None) -> str:
    if since_seq is None:
        return _script_call(app, "minebot_drain_events", bot)
    return _script_call(app, "minebot_events_since", bot, since_seq)


def build_chat_drain_call(bot: str, app: str = SCARPET_APP, *, since_seq: int | None = None) -> str:
    if since_seq is None:
        return _script_call(app, "minebot_drain_chat", bot)
    return _script_call(app, "minebot_chat_since", bot, since_seq)


def build_say_call(bot: str, text: str, app: str = SCARPET_APP) -> str:
    return f"script in {app} run minebot_say({_scarpet_arg(bot)}, {_scarpet_string_arg(text)})"


def build_watch_call(bot: str, app: str = SCARPET_APP) -> str:
    return _script_call(app, "watch_bot", bot)


def build_spawn_call(
    bot: str,
    pos: tuple[int, int, int] | None = None,
    app: str = SCARPET_APP,
    *,
    yaw: float | None = None,
    pitch: float | None = None,
    dimension: str | None = None,
    gamemode: str | None = None,
    emit_respawned: bool = False,
) -> str:
    payload: dict[str, Any] = {}
    if pos is not None:
        payload["pos"] = list(pos)
    if yaw is not None:
        payload["yaw"] = yaw
    if pitch is not None:
        payload["pitch"] = pitch
    if dimension is not None:
        payload["dimension"] = dimension
    if gamemode is not None:
        payload["gamemode"] = gamemode
    if emit_respawned:
        payload["emit_respawned"] = True
    return _script_call(app, "minebot_spawn", bot, payload)


def build_despawn_call(bot: str, app: str = SCARPET_APP) -> str:
    return _script_call(app, "minebot_despawn", bot)


def build_interrupt_call(bot: str, reason: str | None = None, app: str = SCARPET_APP) -> str:
    payload: dict[str, Any] = {}
    if reason:
        payload["reason"] = reason
    return _script_call(app, "minebot_interrupt", bot, payload)


def parse_result(raw: str) -> Result:
    envelope = _parse_envelope(raw)
    _require_type(envelope, "result")
    _require_complete(envelope)
    return Result(
        id=envelope.get("id"),
        bot=str(envelope["bot"]),
        type="result",
        ok=bool(envelope["ok"]),
        accepted=bool(envelope["accepted"]),
        complete=bool(envelope["complete"]),
        data=dict(envelope.get("data") or {}),
        error=envelope.get("error"),
    )


def parse_state(raw: str) -> BodyState:
    envelope = _parse_envelope(raw)
    _require_type(envelope, "state")
    _require_complete(envelope)
    data = dict(envelope.get("data") or {})
    return BodyState.from_envelope_data(
        bot=str(envelope["bot"]),
        complete=bool(envelope["complete"]),
        data=data,
    )


def parse_perception(raw: str) -> PerceptionResult:
    envelope = _parse_envelope(raw)
    _require_type(envelope, "perception")
    return PerceptionResult(
        bot=str(envelope["bot"]),
        scope=str(envelope["scope"]),
        type="perception",
        ok=bool(envelope["ok"]),
        complete=bool(envelope["complete"]),
        data=dict(envelope.get("data") or {}),
        uncertainty=envelope.get("uncertainty"),
        next=envelope.get("next"),
        error=envelope.get("error"),
    )


def parse_events(raw: str) -> list[Event]:
    return parse_events_page(raw)[0]


def parse_events_page(raw: str) -> tuple[list[Event], str | None]:
    envelope = _parse_envelope(raw)
    _require_type(envelope, "events")
    _require_complete(envelope)
    events = []
    for item in envelope.get("events") or []:
        if item.get("type") != "event":
            raise EnvelopeError(f"event entry missing type='event': {item!r}")
        events.append(
            Event(
                seq=int(item["seq"]),
                tick=int(item["tick"]),
                bot=str(item["bot"]),
                name=str(item["name"]),
                data=dict(item.get("data") or {}),
            )
        )
    next_cursor = envelope.get("next")
    return events, None if next_cursor is None else str(next_cursor)


def _script_call(app: str, fn: str, *args: Any) -> str:
    encoded_args = ", ".join(_scarpet_arg(arg) for arg in args)
    return f"script in {app} run {fn}({encoded_args})"


def _scarpet_arg(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if not text.isascii() or any(ord(ch) < 32 or ord(ch) > 126 for ch in text):
        raise ValueError("Scarpet RCON arguments must be ASCII printable after JSON encoding")
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _scarpet_string_arg(value: str) -> str:
    text = value.replace("\r", " ").replace("\n", " ")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("Scarpet string arguments must not contain control characters")
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _parse_envelope(raw: str) -> dict[str, Any]:
    if len(raw) >= RCON_TRUNCATION_LIMIT:
        raise TruncatedPayloadError("RCON response reached the known 4096-char truncation boundary")
    text = _SCRIPT_PREFIX_RE.sub("", raw.strip())
    text = _SCARPET_TIMING_TOKEN_RE.sub(" ", text).strip()
    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        object_start = text.find("{")
        if object_start < 0:
            raise EnvelopeError(f"response is not JSON envelope: {raw[:120]!r}") from exc
        try:
            payload, end = decoder.raw_decode(text[object_start:])
            text = text[object_start:]
        except json.JSONDecodeError as nested_exc:
            raise EnvelopeError(f"response is not JSON envelope: {raw[:120]!r}") from nested_exc
    trailing = text[end:].strip()
    if trailing:
        raise EnvelopeError(f"unexpected content after JSON envelope: {trailing[:80]!r}")
    if not isinstance(payload, dict):
        raise EnvelopeError(f"response envelope must be object, got {type(payload).__name__}")
    return payload


def _require_type(envelope: dict[str, Any], expected: str) -> None:
    actual = envelope.get("type")
    if actual != expected:
        raise EnvelopeError(f"expected envelope type {expected!r}, got {actual!r}")


def _require_complete(envelope: dict[str, Any]) -> None:
    if envelope.get("complete") is not True:
        raise IncompletePayloadError(f"incomplete {envelope.get('type')} payload")
