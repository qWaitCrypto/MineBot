"""Sans-io protocol core for the ``fakeplayer-body/1`` wire contract.

This module owns request construction, response correlation, capability
negotiation, and per-bot event sequencing for the Java Body WebSocket
protocol — with no I/O. A transport shell feeds decoded JSON objects in and
sends built requests out; unit tests and the cross-language fixture suite
drive the same state machine directly.

Contract reference: docs/design-docs/fakeplayer-java-body-protocol.md.
Wire note: the transport stamps ``seq``/``server_tick``/``sent_at_ms`` onto
response frames at the connection level; on ``EVENT`` frames ``seq`` is the
per-bot event sequence instead. The frame ``type`` disambiguates.
"""

from __future__ import annotations

from dataclasses import dataclass, field


PROTOCOL = "fakeplayer-body/1"
CHANNEL = "fakeplayer-body"


class ProtocolViolation(Exception):
    """The peer broke the wire contract; fail closed, never guess."""


class CapabilityGap(Exception):
    """The server does not offer this request type; report, never substitute."""


@dataclass(frozen=True)
class Response:
    request_id: str
    request_type: str
    type: str
    payload: dict


@dataclass(frozen=True)
class ErrorResponse:
    request_id: str | None
    request_type: str | None
    code: str
    message: str
    retryable: bool
    payload: dict


@dataclass(frozen=True)
class BotEvent:
    bot: str
    seq: int
    tick: int
    name: str
    action_id: str | None
    data: dict


@dataclass(frozen=True)
class EventGap:
    bot: str
    from_seq: int
    to_seq: int


@dataclass(frozen=True)
class ServerProposal:
    """A server-initiated MUTATION_PROPOSAL awaiting a governance verdict.

    Fail-closed contract: not answering is a denial after the frozen timeout,
    so a consumer that cannot decide simply does nothing wrong by staying
    silent — but it must never answer allow without the real governance
    decision.
    """

    proposal_id: str
    bot: str
    action_id: str
    kind: str
    x: int
    y: int
    z: int
    block_id: str
    payload: dict
    context: str | None = None


@dataclass
class _Negotiated:
    minecraft_version: str
    max_request_bytes: int
    max_requests_per_second: int
    request_types: tuple[str, ...]


class JavaBodyProtocol:
    """Pure request/response/event state machine for one connection epoch."""

    def __init__(self) -> None:
        self._request_counter = 0
        self._pending: dict[str, str] = {}
        self._last_seq_by_bot: dict[str, int] = {}
        self._negotiated: _Negotiated | None = None

    # ------------------------------------------------------------------
    # Request builders. Each registers the pending request id.
    # ------------------------------------------------------------------

    def hello(self) -> dict:
        return self._request("HELLO", {})

    def find_blocks(
        self,
        bot_name: str,
        block_ids: list[str],
        radius: int,
        *,
        vertical_radius: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict:
        self._require_capability("FIND_BLOCKS")
        body: dict = {"bot_name": bot_name, "block_ids": list(block_ids), "radius": radius}
        if vertical_radius is not None:
            body["vertical_radius"] = vertical_radius
        if limit is not None:
            body["limit"] = limit
        if cursor is not None:
            body["cursor"] = cursor
        return self._request("FIND_BLOCKS", body)

    def navigate(
        self,
        bot_name: str,
        action_id: str,
        goal: dict,
        *,
        timeout_ticks: int | None = None,
    ) -> dict:
        self._require_capability("NAVIGATE")
        body: dict = {"bot_name": bot_name, "action_id": action_id, "goal": dict(goal)}
        if timeout_ticks is not None:
            body["timeout_ticks"] = timeout_ticks
        return self._request("NAVIGATE", body)

    def cancel_action(self, action_id: str) -> dict:
        self._require_capability("CANCEL_ACTION")
        return self._request("CANCEL_ACTION", {"action_id": action_id})

    def query_action(self, action_id: str) -> dict:
        self._require_capability("QUERY_ACTION")
        return self._request("QUERY_ACTION", {"action_id": action_id})

    def resume_events(self, bot_name: str, after_seq: int) -> dict:
        self._require_capability("RESUME_EVENTS")
        return self._request("RESUME_EVENTS", {"bot_name": bot_name, "after_seq": after_seq})

    def body_state(self, bot_name: str) -> dict:
        self._require_capability("BODY_STATE")
        return self._request("BODY_STATE", {"bot_name": bot_name})

    def collect_block(
        self,
        bot_name: str,
        action_id: str,
        block_ids: list[str],
        *,
        radius: int | None = None,
        vertical_radius: int | None = None,
        timeout_ticks: int | None = None,
    ) -> dict:
        self._require_capability("COLLECT_BLOCK")
        body: dict = {"bot_name": bot_name, "action_id": action_id, "block_ids": list(block_ids)}
        if radius is not None:
            body["radius"] = radius
        if vertical_radius is not None:
            body["vertical_radius"] = vertical_radius
        if timeout_ticks is not None:
            body["timeout_ticks"] = timeout_ticks
        return self._request("COLLECT_BLOCK", body)

    def ascend(
        self,
        bot_name: str,
        action_id: str,
        *,
        target_y: int | None = None,
        timeout_ticks: int | None = None,
    ) -> dict:
        self._require_capability("ASCEND")
        body: dict = {"bot_name": bot_name, "action_id": action_id}
        if target_y is not None:
            body["target_y"] = target_y
        if timeout_ticks is not None:
            body["timeout_ticks"] = timeout_ticks
        return self._request("ASCEND", body)

    def mutation_verdict(self, proposal_id: str, allow: bool, reason: str) -> dict:
        """Fire-and-forget by design: a lost verdict times out into a denial."""
        self._require_capability("MUTATION_VERDICT")
        return {
            "channel": CHANNEL,
            "protocol": PROTOCOL,
            "type": "MUTATION_VERDICT",
            "proposal_id": proposal_id,
            "allow": allow,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Ingest. Feed one decoded frame; receive typed items in order.
    # ------------------------------------------------------------------

    def feed(self, message: dict) -> list[Response | ErrorResponse | BotEvent | EventGap | ServerProposal]:
        if not isinstance(message, dict):
            raise ProtocolViolation("frame must be a JSON object")
        channel = message.get("channel")
        if channel == "transport":
            # Transport-level errors (rate limit, size, parse) carry no
            # application channel; anything else on "transport" is a breach.
            if message.get("type") != "ERROR":
                raise ProtocolViolation("transport channel only carries ERROR frames")
            return [self._feed_error(message)]
        if channel != CHANNEL:
            raise ProtocolViolation(f"unexpected channel: {channel!r}")
        frame_type = message.get("type")
        if frame_type == "EVENT":
            return self._feed_event(message)
        if frame_type == "ERROR":
            return [self._feed_error(message)]
        if frame_type == "MUTATION_PROPOSAL":
            return [self._feed_proposal(message)]
        return self._feed_response(message)

    # -- responses ------------------------------------------------------

    def _feed_response(self, message: dict) -> list[Response | BotEvent | EventGap]:
        frame_type = message.get("type")
        request_id = message.get("request_id")
        if not isinstance(frame_type, str) or not isinstance(request_id, str):
            raise ProtocolViolation("response frames need type and request_id")
        request_type = self._pending.pop(request_id, None)
        if request_type is None:
            raise ProtocolViolation(f"response for unknown request_id {request_id!r}")
        # Acknowledgement-shaped responses are deliberately not "_RESULT":
        # accepting an action is never its terminal result.
        expected = {
            "HELLO": "HELLO_ACK",
            "NAVIGATE": "NAVIGATE_ACK",
            "COLLECT_BLOCK": "COLLECT_BLOCK_ACK",
            "ASCEND": "ASCEND_ACK",
        }.get(request_type, f"{request_type}_RESULT")
        if frame_type != expected:
            raise ProtocolViolation(f"{request_type} answered by {frame_type}")
        if frame_type == "HELLO_ACK":
            self._accept_hello(message)
        items: list[Response | BotEvent | EventGap] = []
        if frame_type == "RESUME_EVENTS_RESULT":
            items.extend(self._ingest_replay(message))
        items.insert(0, Response(request_id, request_type, frame_type, dict(message)))
        return items

    def _accept_hello(self, message: dict) -> None:
        if message.get("protocol") != PROTOCOL:
            raise ProtocolViolation(f"server speaks {message.get('protocol')!r}, need {PROTOCOL!r}")
        request_types = message.get("request_types")
        if not isinstance(request_types, list) or not all(isinstance(t, str) for t in request_types):
            raise ProtocolViolation("HELLO_ACK request_types must be a string array")
        self._negotiated = _Negotiated(
            minecraft_version=str(message.get("minecraft_version")),
            max_request_bytes=int(message.get("max_request_bytes", 0)),
            max_requests_per_second=int(message.get("max_requests_per_second", 0)),
            request_types=tuple(request_types),
        )

    def _feed_proposal(self, message: dict) -> ServerProposal:
        proposal_id = message.get("proposal_id")
        bot = message.get("bot")
        action_id = message.get("action_id")
        mutation = message.get("mutation")
        if not isinstance(proposal_id, str) or not isinstance(bot, str) or not isinstance(mutation, dict):
            raise ProtocolViolation("MUTATION_PROPOSAL needs proposal_id, bot, and mutation")
        kind = mutation.get("kind")
        block_id = mutation.get("block_id")
        if not isinstance(kind, str) or not isinstance(block_id, str):
            raise ProtocolViolation("MUTATION_PROPOSAL mutation needs kind and block_id")
        try:
            x = int(mutation["x"])
            y = int(mutation["y"])
            z = int(mutation["z"])
        except (KeyError, TypeError, ValueError) as error:
            raise ProtocolViolation("MUTATION_PROPOSAL mutation needs integer coordinates") from error
        return ServerProposal(
            proposal_id=proposal_id,
            bot=bot,
            action_id=action_id if isinstance(action_id, str) else "",
            kind=kind,
            x=x,
            y=y,
            z=z,
            block_id=block_id,
            payload=dict(message),
            context=(
                str(mutation["context"])
                if isinstance(mutation.get("context"), str)
                else None
            ),
        )

    def _feed_error(self, message: dict) -> ErrorResponse:
        request_id = message.get("request_id")
        request_type = None
        if isinstance(request_id, str):
            request_type = self._pending.pop(request_id, None)
        code = message.get("code")
        if not isinstance(code, str):
            raise ProtocolViolation("ERROR frames need a string code")
        return ErrorResponse(
            request_id=request_id if isinstance(request_id, str) else None,
            request_type=request_type,
            code=code,
            message=str(message.get("message", "")),
            retryable=bool(message.get("retryable", False)),
            payload=dict(message),
        )

    # -- events ---------------------------------------------------------

    def _feed_event(self, message: dict) -> list[BotEvent | EventGap]:
        event = self._parse_event(message)
        return self._sequence_event(event)

    def _ingest_replay(self, message: dict) -> list[BotEvent | EventGap]:
        items: list[BotEvent | EventGap] = []
        bot = message.get("bot")
        gap = message.get("event_gap")
        if isinstance(gap, dict) and isinstance(bot, str):
            from_seq = int(gap.get("from", 0))
            to_seq = int(gap.get("to", 0))
            items.append(EventGap(bot, from_seq, to_seq))
            self._last_seq_by_bot[bot] = max(self._last_seq_by_bot.get(bot, 0), to_seq)
        events = message.get("events")
        if not isinstance(events, list):
            raise ProtocolViolation("RESUME_EVENTS_RESULT events must be an array")
        for raw in events:
            if not isinstance(raw, dict):
                raise ProtocolViolation("replayed events must be objects")
            items.extend(self._sequence_event(self._parse_event(raw)))
        return items

    def _parse_event(self, message: dict) -> BotEvent:
        bot = message.get("bot")
        seq = message.get("seq")
        tick = message.get("tick")
        name = message.get("event")
        if not isinstance(bot, str) or not isinstance(seq, int) or not isinstance(name, str):
            raise ProtocolViolation("EVENT frames need bot, integer seq, and event name")
        action_id = message.get("action_id")
        data = message.get("data")
        return BotEvent(
            bot=bot,
            seq=seq,
            tick=int(tick) if isinstance(tick, int) else 0,
            name=name,
            action_id=action_id if isinstance(action_id, str) else None,
            data=dict(data) if isinstance(data, dict) else {},
        )

    def _sequence_event(self, event: BotEvent) -> list[BotEvent | EventGap]:
        last = self._last_seq_by_bot.get(event.bot, 0)
        if event.seq <= last:
            # Duplicate delivery (e.g. replay overlap): drop silently is a
            # lie; drop *visibly* by returning nothing but keeping order
            # facts intact — duplicates carry no new information.
            return []
        items: list[BotEvent | EventGap] = []
        if event.seq > last + 1:
            items.append(EventGap(event.bot, last + 1, event.seq - 1))
        self._last_seq_by_bot[event.bot] = event.seq
        items.append(event)
        return items

    # ------------------------------------------------------------------
    # Introspection.
    # ------------------------------------------------------------------

    @property
    def negotiated(self) -> bool:
        return self._negotiated is not None

    def supports(self, request_type: str) -> bool:
        return self._negotiated is not None and request_type in self._negotiated.request_types

    def last_seq(self, bot_name: str) -> int:
        return self._last_seq_by_bot.get(bot_name, 0)

    def pending_request_ids(self) -> tuple[str, ...]:
        return tuple(self._pending)

    # ------------------------------------------------------------------

    def _require_capability(self, request_type: str) -> None:
        if self._negotiated is None:
            raise CapabilityGap("HELLO negotiation has not completed")
        if request_type not in self._negotiated.request_types:
            raise CapabilityGap(f"server does not offer {request_type}")

    def _request(self, request_type: str, body: dict) -> dict:
        self._request_counter += 1
        request_id = f"r-{self._request_counter}"
        message = {
            "channel": CHANNEL,
            "protocol": PROTOCOL,
            "type": request_type,
            "request_id": request_id,
            **body,
        }
        self._pending[request_id] = request_type
        return message
