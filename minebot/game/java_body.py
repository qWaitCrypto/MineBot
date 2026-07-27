"""JavaBody: the neutral ``Body``-contract face of the Java Body provider.

This is the abstraction seam the multi-provider design promises: the same
``minebot.contract.Body`` protocol that ScarpetBody implements, backed by the
``fakeplayer-body/1`` wire protocol. Reads are wire-native (BODY_STATE,
FIND_BLOCKS, world facts, inventory, and the pushed event stream); physical objectives delegate whole
actions to the Java Body (navigate/collect), while ordinary player controls
(``selectItem``/``moveItem``/``dropItem``/``stop``/``lookAt``/``useItem``)
retain the neutral contract's
terminal events; semantics the provider does not
offer return a **typed capability gap** — never a silent fallback to weaker
behavior, per the Body-layer capability-negotiation rule.

Inventory, block/entity perception, and the first player-control family are
live; remaining data-plane primitives stay explicit gaps.

Hybrid deployments keep ScarpetBody for the scopes and actions still owned by
the Scarpet path; this class is how the Java provider grows into the full
contract surface one honest capability at a time.
"""

from __future__ import annotations

from minebot.contract import (
    Action,
    BodyState,
    Event,
    JsonObject,
    PerceptionResult,
    Result,
)
from minebot.game.java_body_adapter import JavaBodyClient
from minebot.game.java_body_protocol import BotEvent, ErrorResponse, Response

_CAPABILITY_GAP = "capability_unavailable"
_WORLD_READ_SCOPES = frozenset({
    "blockAt",
    "blockCells",
    "surfaceColumns",
    "nearbyBlocks",
    "debugBlocks",
})
_ENTITY_READ_SCOPES = frozenset({"nearbyEntities"})
_PLAYER_ACTION_TERMINALS = {
    "dropItem": "dropDone",
    "lookAt": "lookDone",
    "moveItem": "moveItemDone",
    "selectItem": "selectItemDone",
    "stop": "stopDone",
    "useItem": "useDone",
}


class JavaBody:
    """Neutral Body-contract adapter over :class:`JavaBodyClient`."""

    def __init__(self, client: JavaBodyClient, bot_name: str) -> None:
        self._client = client
        self.bot_name = bot_name
        self._action_terminals: dict[str, Event] = {}

    # -- reads (wire-native) --------------------------------------------

    def get_state(self) -> BodyState:
        reply = self._client.request_response(lambda p: p.body_state(self.bot_name))
        if isinstance(reply, ErrorResponse) or reply.payload.get("missing") is True:
            return _missing_state(self.bot_name)
        payload = reply.payload
        position = payload.get("position") or {}
        counts = payload.get("inventory_counts") or {}
        return BodyState(
            bot=self.bot_name,
            pos=(float(position.get("x", 0.0)), float(position.get("y", 0.0)), float(position.get("z", 0.0))),
            yaw=_opt_float(payload.get("yaw")),
            pitch=_opt_float(payload.get("pitch")),
            health=float(payload.get("health", 0.0)),
            food=int(payload.get("food", 0)),
            oxygen=int(payload["air"]) if isinstance(payload.get("air"), int) else None,
            inventory_raw="",
            inventory_hash="",
            effects=None,
            time=int(payload.get("game_time", 0)),
            weather=None,
            dimension=payload.get("dimension"),
            complete=True,
            missing=False,
            inventory_counts={str(k): int(v) for k, v in counts.items()},
            selected_item=payload.get("selected_item"),
            offhand_item=payload.get("offhand_item"),
            body_owner=payload.get("body_owner"),
        )

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        if scope == "inventory":
            return self._perceive_inventory(params)
        if scope in _WORLD_READ_SCOPES:
            return self._perceive_world(scope, params)
        if scope in _ENTITY_READ_SCOPES:
            return self._perceive_entities(scope, params)
        if scope == "findBlocks":
            return self._perceive_find_blocks(params)
        return PerceptionResult(
            bot=self.bot_name,
            scope=scope,
            type="perception",
            ok=False,
            complete=True,
            error=f"{_CAPABILITY_GAP}:{scope}",
        )

    def _perceive_find_blocks(
        self,
        params: dict[str, object],
    ) -> PerceptionResult:
        block_ids = _find_block_ids(params)
        if not block_ids:
            return PerceptionResult(
                bot=self.bot_name,
                scope="findBlocks",
                type="perception",
                ok=False,
                complete=True,
                error="invalid_request:no_block_types",
            )
        start = _opt_int(params.get("start"))
        cursor = params.get("cursor") if isinstance(params.get("cursor"), str) else None
        if cursor is None and start not in {None, 0}:
            return PerceptionResult(
                bot=self.bot_name,
                scope="findBlocks",
                type="perception",
                ok=False,
                complete=True,
                error="invalid_cursor:numeric_resume_requires_original_snapshot",
            )
        reply = self._client.request_response(lambda p: p.find_blocks(
            self.bot_name,
            block_ids,
            int(params.get("radius", 32)),
            vertical_radius=_find_vertical_radius(params),
            limit=_opt_int(params.get("limit")),
            cursor=cursor,
        ))
        if isinstance(reply, ErrorResponse):
            missing = reply.code in {"body_missing", "missing_body"}
            return PerceptionResult(
                bot=self.bot_name,
                scope="findBlocks",
                type="perception",
                ok=False,
                complete=missing,
                uncertainty=[{"reason": "missing_body"}] if missing else None,
                error="missing_body" if missing else reply.code,
            )
        payload = reply.payload
        next_cursor = payload.get("next_cursor")
        coverage_complete = bool(payload.get("coverage_complete"))
        result_capped = bool(payload.get("result_capped"))
        unloaded_chunks = int(payload.get("unloaded_chunk_count") or 0)
        complete = next_cursor is None and coverage_complete and not result_capped
        uncertainty: list[dict[str, object]] = []
        if next_cursor is not None:
            uncertainty.append({"reason": "page_limit"})
        if unloaded_chunks:
            uncertainty.append({
                "reason": "unloaded_boundary",
                "unloaded_chunk_count": unloaded_chunks,
            })
        if result_capped:
            uncertainty.append({"reason": "result_capped"})
        blocks = [
            {
                "x": int(item["x"]),
                "y": int(item["y"]),
                "z": int(item["z"]),
                "type": str(item.get("block_id") or "unknown"),
                "state": str(item.get("state") or "UNKNOWN"),
                "dist2": float(item.get("distance_squared") or 0.0),
            }
            for item in payload.get("matches", [])
            if isinstance(item, dict)
        ]
        return PerceptionResult(
            bot=self.bot_name,
            scope="findBlocks",
            type="perception",
            ok=True,
            complete=complete,
            data={
                "start": int(payload.get("start") or 0),
                "limit": int(params.get("limit") or 32),
                "count": len(blocks),
                "totalMatches": int(payload.get("total_matches") or len(blocks)),
                "nextStart": next_cursor,
                "blocks": blocks,
                "index_generation": payload.get("index_generation"),
                "unloaded_chunk_count": unloaded_chunks,
                "result_capped": result_capped,
                "serverCostMicros": payload.get("server_cost_micros"),
            },
            uncertainty=uncertainty,
            next=str(next_cursor) if next_cursor is not None else None,
        )

    def _perceive_inventory(self, params: dict[str, object]) -> PerceptionResult:
        reply = self._client.request_response(lambda p: p.inventory(
            self.bot_name,
            start=_opt_int(params.get("start")),
            limit=_opt_int(params.get("limit")),
        ))
        if isinstance(reply, ErrorResponse):
            missing = reply.code in {"body_missing", "missing_body"}
            return PerceptionResult(
                bot=self.bot_name,
                scope="inventory",
                type="perception",
                ok=False,
                complete=missing,
                uncertainty=[{"reason": "missing_body"}] if missing else None,
                error="missing_body" if missing else reply.code,
            )
        payload = reply.payload
        if payload.get("missing") is True:
            return PerceptionResult(
                bot=self.bot_name,
                scope="inventory",
                type="perception",
                ok=False,
                complete=True,
                uncertainty=[{"reason": "missing_body"}],
                error="missing_body",
            )
        next_start = payload.get("nextStart")
        complete = next_start is None
        return PerceptionResult(
            bot=self.bot_name,
            scope="inventory",
            type="perception",
            ok=True,
            complete=complete,
            data={
                "start": payload.get("start"),
                "limit": payload.get("limit"),
                "nextStart": next_start,
                "totalSlots": payload.get("totalSlots"),
                "slots": payload.get("slots", []),
            },
            uncertainty=[] if complete else [{"reason": "page_limit"}],
            next=None if complete else str(next_start),
        )

    def _perceive_world(
        self,
        scope: str,
        params: dict[str, object],
    ) -> PerceptionResult:
        reply = self._client.request_response(
            lambda protocol: protocol.world_read(self.bot_name, scope, params)
        )
        if isinstance(reply, ErrorResponse):
            missing = reply.code in {"body_missing", "missing_body"}
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=False,
                complete=missing,
                uncertainty=[{"reason": "missing_body"}] if missing else None,
                error="missing_body" if missing else reply.code,
            )
        payload = reply.payload
        if payload.get("missing") is True:
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=False,
                complete=True,
                uncertainty=[{"reason": "missing_body"}],
                error="missing_body",
            )
        data = dict(payload.get("data") or {})
        if payload.get("server_cost_micros") is not None:
            data["serverCostMicros"] = int(payload["server_cost_micros"])
        return PerceptionResult(
            bot=self.bot_name,
            scope=scope,
            type="perception",
            ok=bool(payload.get("ok", True)),
            complete=bool(payload.get("complete")),
            data=data,
            uncertainty=list(payload.get("uncertainty") or []),
            next=str(payload["next"]) if payload.get("next") is not None else None,
            error=str(payload["error"]) if payload.get("error") is not None else None,
        )

    def _perceive_entities(
        self,
        scope: str,
        params: dict[str, object],
    ) -> PerceptionResult:
        reply = self._client.request_response(
            lambda protocol: protocol.entity_read(self.bot_name, scope, params)
        )
        if isinstance(reply, ErrorResponse):
            missing = reply.code in {"body_missing", "missing_body"}
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=False,
                complete=missing,
                uncertainty=[{"reason": "missing_body"}] if missing else None,
                error="missing_body" if missing else reply.code,
            )
        payload = reply.payload
        if payload.get("missing") is True:
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=False,
                complete=True,
                uncertainty=[{"reason": "missing_body"}],
                error="missing_body",
            )
        data = dict(payload.get("data") or {})
        if payload.get("server_cost_micros") is not None:
            data["serverCostMicros"] = int(payload["server_cost_micros"])
        return PerceptionResult(
            bot=self.bot_name,
            scope=scope,
            type="perception",
            ok=bool(payload.get("ok", True)),
            complete=bool(payload.get("complete")),
            data=data,
            uncertainty=list(payload.get("uncertainty") or []),
            next=str(payload["next"]) if payload.get("next") is not None else None,
            error=str(payload["error"]) if payload.get("error") is not None else None,
        )

    def poll_events(self) -> list[Event]:
        return [_contract_event(item) for item in self._client.drain_events()]

    # -- whole-objective writes -----------------------------------------

    def execute(self, action: Action) -> Result:
        if action.name == "navigate":
            outcome = self._client.navigate(dict(action.params.get("goal") or {}),
                                            timeout_ticks=_opt_int(action.params.get("timeout_ticks")))
        elif action.name == "collectBlock":
            outcome = self._client.collect_block(
                [str(b) for b in (action.params.get("block_types") or ())],
                radius=_opt_int(action.params.get("radius")),
                timeout_ticks=_opt_int(action.params.get("timeout_ticks")),
            )
        elif action.name == "ascend":
            outcome = self._client.ascend(
                target_y=_opt_int(action.params.get("target_y")),
                timeout_ticks=_opt_int(action.params.get("timeout_ticks")),
            )
        elif action.name in _PLAYER_ACTION_TERMINALS:
            return self._execute_player_action(action)
        else:
            return _gap_result(action, self.bot_name)
        return Result(
            id=action.id,
            bot=self.bot_name,
            type="result",
            ok=outcome.success,
            accepted=True,
            complete=True,
            data=dict(outcome.metrics or {}),
            error=None if outcome.success else outcome.reason,
        )

    # -- semantics the Java provider does not offer yet ------------------

    def spawn(self, pos=None, *, yaw=None, pitch=None, dimension=None, gamemode=None, emit_respawned=False) -> Result:
        return _gap_result(Action.create("spawn"), self.bot_name)

    def despawn(self) -> Result:
        return _gap_result(Action.create("despawn"), self.bot_name)

    def await_action_terminal(self, action_id, timeout_s=15.0, poll_interval_s=0.10,
                              terminal_events=None, intermediate_events=None) -> Event:
        terminal = self._action_terminals.pop(str(action_id), None)
        if terminal is None:
            raise NotImplementedError(f"{_CAPABILITY_GAP}:await_action_terminal:{action_id}")
        if terminal_events is not None and terminal.name not in terminal_events:
            raise ValueError(f"unexpected Java terminal {terminal.name} for {action_id}")
        return terminal

    def ignite_block(self, pos, *, item=None, allow_server_substitute=False, timeout_s=8.0) -> Event:
        raise NotImplementedError(f"{_CAPABILITY_GAP}:ignite_block")

    def sow_crop(self, pos, *, crop_block, seed_item=None, allow_server_substitute=False, timeout_s=8.0) -> Event:
        raise NotImplementedError(f"{_CAPABILITY_GAP}:sow_crop")

    def interrupt(self, reason: str | None = None) -> Result:
        return _gap_result(Action.create("interrupt"), self.bot_name)

    def _execute_player_action(self, action: Action) -> Result:
        outcome = self._client.player_action(action.id, action.name, dict(action.params))
        terminal_record = self._client.last_action_terminal
        if terminal_record is None or terminal_record[0] != action.id:
            return Result(
                id=action.id,
                bot=self.bot_name,
                type="result",
                ok=False,
                accepted=False,
                complete=True,
                data=dict(outcome.metrics or {}),
                error=outcome.reason,
            )

        _, terminal_facts = terminal_record
        terminal_event = next(
            (
                event
                for event in reversed(self._client.last_action_events)
                if event.name == "action_terminal" and event.action_id == action.id
            ),
            None,
        )
        data = dict(terminal_facts)
        data.update({
            "action_id": action.id,
            "success": outcome.success,
            "stopped_reason": outcome.reason,
        })
        self._action_terminals[action.id] = Event(
            seq=terminal_event.seq if terminal_event is not None else 0,
            tick=terminal_event.tick if terminal_event is not None else 0,
            bot=self.bot_name,
            name=_PLAYER_ACTION_TERMINALS[action.name],
            data=data,
        )
        return Result(
            id=action.id,
            bot=self.bot_name,
            type="result",
            ok=True,
            accepted=True,
            complete=False,
            data={"action": action.name},
        )


def _contract_event(item: BotEvent) -> Event:
    return Event(seq=item.seq, tick=item.tick, bot=item.bot, name=item.name, data=dict(item.data))


def _gap_result(action: Action, bot: str) -> Result:
    return Result(
        id=action.id, bot=bot, type="result",
        ok=False, accepted=False, complete=True,
        error=f"{_CAPABILITY_GAP}:{action.name}",
    )


def _missing_state(bot: str) -> BodyState:
    return BodyState(
        bot=bot, pos=(0.0, 0.0, 0.0), yaw=None, pitch=None,
        health=0.0, food=0, oxygen=None, inventory_raw="", inventory_hash="",
        effects=None, time=0, weather=None, dimension=None,
        complete=False, missing=True,
    )


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _opt_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _find_block_ids(params: dict[str, object]) -> list[str]:
    raw = params.get("block_ids")
    if not isinstance(raw, (list, tuple)):
        raw = params.get("types")
    if not isinstance(raw, (list, tuple)):
        one = params.get("type")
        raw = [one] if isinstance(one, str) and one else []
    normalized: list[str] = []
    for value in raw:
        block_id = str(value).strip()
        if not block_id:
            continue
        if ":" not in block_id:
            block_id = f"minecraft:{block_id}"
        if block_id not in normalized:
            normalized.append(block_id)
    return normalized


def _find_vertical_radius(params: dict[str, object]) -> int | None:
    for key in ("vertical_radius", "y_radius", "yRadius"):
        value = _opt_int(params.get(key))
        if value is not None:
            return value
    return None
