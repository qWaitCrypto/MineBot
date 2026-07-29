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

import time

from minebot.contract import (
    Action,
    BodyState,
    Event,
    JsonObject,
    PerceptionResult,
    Result,
)
from minebot.game.java_body_adapter import JavaBodyClient
from minebot.game.chat import sanitize_chat_text
from minebot.game.java_body_protocol import BotEvent, ErrorResponse, Response

_CAPABILITY_GAP = "capability_unavailable"
_WORLD_READ_SCOPES = frozenset({
    "blockAt",
    "blockCells",
    "surfaceColumns",
    "nearbyBlocks",
    "debugBlocks",
})
_ENTITY_READ_SCOPES = frozenset({"nearbyEntities", "nearbyHostiles"})
_ACTION_TERMINALS = {
    "containerTransfer": "containerDone",
    "craftItem": "craftDone",
    "engageEntity": "engageDone",
    "followEntity": "followDone",
    "furnaceTransfer": "furnaceDone",
    "handoffItem": "handoffDone",
    "igniteBlock": "igniteDone",
    "jump": "jumpDone",
    "mineBlock": "mineDone",
    "placeBlock": "placeDone",
    "dropItem": "dropDone",
    "lookAt": "lookDone",
    "moveItem": "moveItemDone",
    "selectItem": "selectItemDone",
    "stop": "stopDone",
    "sowCrop": "sowDone",
    "useItem": "useDone",
}


class JavaBody:
    """Neutral Body-contract adapter over :class:`JavaBodyClient`."""

    # The Java protocol can return the complete neutral 46-slot view in one
    # response. Scarpet has no attribute here and keeps its legacy 12-slot
    # transaction pages.
    preferred_inventory_page_size = 46

    def __init__(
        self,
        client: JavaBodyClient,
        bot_name: str,
        *,
        read_client: JavaBodyClient | None = None,
    ) -> None:
        self._client = client
        self._read_client = read_client or client
        self.bot_name = bot_name
        self._action_terminals: dict[str, Event] = {}
        self.last_seq = 0
        self.last_chat_seq = 0
        self.event_log: list[Event] = []
        self._server_epoch: str | None = None

    # -- reads (wire-native) --------------------------------------------

    def get_state(self) -> BodyState:
        reply = self._read_client.request_response(lambda p: p.body_state(self.bot_name))
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
            inventory_raw=str(payload.get("inventory_raw") or ""),
            inventory_hash=str(payload.get("inventory_hash") or ""),
            effects=list(payload.get("effects") or []),
            time=int(payload.get("game_time", 0)),
            weather=str(payload["weather"]) if payload.get("weather") is not None else None,
            dimension=payload.get("dimension"),
            complete=True,
            sleeping=bool(payload.get("sleeping")),
            missing=False,
            selected_slot=_opt_int(payload.get("selected_slot")),
            inventory_counts={str(k): int(v) for k, v in counts.items()},
            selected_item=payload.get("selected_item"),
            offhand_item=payload.get("offhand_item"),
            body_owner=payload.get("body_owner"),
            pending_action_count=_opt_int(payload.get("pending_action_count")),
            hazard_unresolved=(
                dict(payload["hazard_unresolved"])
                if isinstance(payload.get("hazard_unresolved"), dict)
                else None
            ),
        )

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        if scope == "inventory":
            return self._perceive_inventory(params)
        if scope == "container":
            return self._perceive_container(params)
        if scope == "recipeData":
            return self._perceive_recipe(params)
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
        reply = self._read_client.request_response(lambda p: p.find_blocks(
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
                complete=missing or not reply.retryable,
                uncertainty=(
                    [{"reason": "missing_body"}]
                    if missing
                    else [
                        {
                            "reason": reply.code,
                            "message": reply.message,
                            "retryable": reply.retryable,
                        }
                    ]
                ),
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
        reply = self._read_client.request_response(lambda p: p.inventory(
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

    def _perceive_container(self, params: dict[str, object]) -> PerceptionResult:
        raw_pos = params.get("pos")
        if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) != 3:
            return PerceptionResult(
                bot=self.bot_name,
                scope="container",
                type="perception",
                ok=False,
                complete=True,
                error="invalid_request:pos",
            )
        try:
            pos = [int(value) for value in raw_pos]
        except (TypeError, ValueError):
            return PerceptionResult(
                bot=self.bot_name,
                scope="container",
                type="perception",
                ok=False,
                complete=True,
                error="invalid_request:pos",
            )
        reply = self._read_client.request_response(lambda protocol: protocol.container_read(
            self.bot_name,
            pos,
            start=_opt_int(params.get("start")),
            limit=_opt_int(params.get("limit")),
        ))
        if isinstance(reply, ErrorResponse):
            missing = reply.code in {"body_missing", "missing_body"}
            return PerceptionResult(
                bot=self.bot_name,
                scope="container",
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
                scope="container",
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
            scope="container",
            type="perception",
            ok=True,
            complete=complete,
            data={
                "pos": payload.get("pos", pos),
                "start": payload.get("start"),
                "limit": payload.get("limit"),
                "nextStart": next_start,
                "totalSlots": payload.get("totalSlots"),
                "slots": payload.get("slots", []),
            },
            uncertainty=[] if complete else [{"reason": "page_limit"}],
            next=None if complete else str(next_start),
        )

    def _perceive_recipe(self, params: dict[str, object]) -> PerceptionResult:
        item = str(params.get("item") or "")
        if not item:
            return PerceptionResult(
                bot=self.bot_name,
                scope="recipeData",
                type="perception",
                ok=False,
                complete=True,
                error="invalid_request:item_required",
            )
        normalized_item = item if ":" in item else f"minecraft:{item}"
        requested_type = str(params.get("type") or "crafting")
        reply = self._read_client.request_response(lambda protocol: protocol.recipe_read(
            self.bot_name,
            normalized_item,
            recipe_type=requested_type,
        ))
        if isinstance(reply, ErrorResponse):
            missing = reply.code in {"body_missing", "missing_body"}
            return PerceptionResult(
                bot=self.bot_name,
                scope="recipeData",
                type="perception",
                ok=False,
                complete=missing or not reply.retryable,
                uncertainty=[{"reason": "missing_body"}] if missing else None,
                error="missing_body" if missing else reply.code,
            )
        payload = reply.payload
        if payload.get("missing") is True:
            return PerceptionResult(
                bot=self.bot_name,
                scope="recipeData",
                type="perception",
                ok=False,
                complete=True,
                uncertainty=[{"reason": "missing_body"}],
                error="missing_body",
            )
        variants = [dict(variant) for variant in payload.get("variants") or [] if isinstance(variant, dict)]
        if payload.get("found") is not True or not variants:
            return PerceptionResult(
                bot=self.bot_name,
                scope="recipeData",
                type="perception",
                ok=False,
                complete=True,
                data={
                    "item": normalized_item,
                    "type": requested_type,
                    "variantCount": 0,
                    "variants": [],
                },
                error="recipe_not_found",
            )
        return PerceptionResult(
            bot=self.bot_name,
            scope="recipeData",
            type="perception",
            ok=True,
            complete=True,
            data={
                "item": str(payload.get("item") or normalized_item),
                "type": str(payload.get("recipe_type") or requested_type),
                "variantCount": int(payload.get("variant_count") or len(variants)),
                "variants": variants,
                "serverCostMicros": int(payload.get("server_cost_micros") or 0),
            },
        )

    def _perceive_world(
        self,
        scope: str,
        params: dict[str, object],
    ) -> PerceptionResult:
        reply = self._read_client.request_response(
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
        reply = self._read_client.request_response(
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
        reply = self._client.resume_events(self.last_seq)
        if isinstance(reply, ErrorResponse):
            raise RuntimeError(f"Java Body event replay failed: {reply.code}")
        normalized: list[Event] = []
        for gap in self._client.drain_event_gaps():
            if gap.bot != self.bot_name or gap.to_seq <= self.last_seq:
                continue
            normalized.append(Event(
                seq=max(self.last_seq + 1, gap.to_seq),
                tick=0,
                bot=self.bot_name,
                name="desync",
                data={"expected_seq": gap.from_seq, "observed_seq": gap.to_seq + 1},
            ))
            self.last_seq = max(self.last_seq, gap.to_seq)
        for item in self._client.drain_events():
            if item.bot != self.bot_name or item.seq <= self.last_seq:
                continue
            event = _contract_event(item)
            normalized.append(event)
            self.last_seq = event.seq
        self.event_log.extend(normalized)
        return normalized

    def event_head(self, proposed_epoch: str) -> dict[str, object]:
        reply = self._client.request_response(
            lambda protocol: protocol.event_head(self.bot_name, proposed_epoch)
        )
        if isinstance(reply, ErrorResponse):
            raise RuntimeError(f"Java Body event head failed: {reply.code}")
        payload = reply.payload
        epoch = str(payload.get("epoch") or "")
        if not epoch:
            raise RuntimeError("Java Body event head is missing epoch")
        self._server_epoch = epoch
        return {
            "event_seq": int(payload.get("event_seq") or 0),
            "chat_seq": int(payload.get("chat_seq") or 0),
            "tick": int(payload.get("tick") or 0),
            "epoch": epoch,
            "owner": payload.get("owner"),
            "pending_action_count": int(payload.get("pending_action_count") or 0),
        }

    def world_identity(self) -> str:
        reply = self._read_client.request_response(lambda protocol: protocol.world_identity())
        if isinstance(reply, ErrorResponse):
            raise RuntimeError(f"Java Body world identity failed: {reply.code}")
        world_id = str(reply.payload.get("world_id") or "")
        if not world_id:
            raise RuntimeError("Java Body world identity is missing")
        return world_id

    def poll_chat_events(self) -> list[Event]:
        reply = self._read_client.request_response(
            lambda protocol: protocol.chat_events(self.bot_name, self.last_chat_seq)
        )
        if isinstance(reply, ErrorResponse):
            raise RuntimeError(f"Java Body chat poll failed: {reply.code}")
        payload = reply.payload
        epoch = str(payload.get("epoch") or "")
        if not epoch or (self._server_epoch is not None and epoch != self._server_epoch):
            raise RuntimeError("Java Body chat epoch changed without an event-head handshake")
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            raise RuntimeError("Java Body chat response is missing events")
        normalized: list[Event] = []
        gap = payload.get("event_gap")
        if isinstance(gap, dict):
            gap_from = int(gap.get("from") or 0)
            gap_to = int(gap.get("to") or 0)
            if gap_to >= gap_from > 0:
                normalized.append(Event(
                    seq=gap_to,
                    tick=0,
                    bot=self.bot_name,
                    name="chatDesync",
                    data={"from_seq": gap_from, "to_seq": gap_to},
                ))
                self.last_chat_seq = max(self.last_chat_seq, gap_to)
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise RuntimeError("Java Body chat event is not an object")
            seq = int(raw.get("seq") or 0)
            if seq <= self.last_chat_seq:
                continue
            data = raw.get("data")
            if (
                raw.get("bot") != self.bot_name
                or raw.get("event") != "agentChat"
                or not isinstance(data, dict)
                or not isinstance(data.get("sender"), str)
                or not isinstance(data.get("message"), str)
            ):
                raise RuntimeError("Java Body chat event is malformed")
            event = Event(
                seq=seq,
                tick=int(raw.get("tick") or 0),
                bot=self.bot_name,
                name="agentChat",
                data=dict(data),
            )
            normalized.append(event)
            self.last_chat_seq = seq
        self.event_log.extend(normalized)
        return normalized

    def say(self, text: str) -> bool:
        cleaned = sanitize_chat_text(text)
        if not cleaned:
            return True
        reply = self._read_client.request_response(
            lambda protocol: protocol.say(self.bot_name, cleaned)
        )
        return not isinstance(reply, ErrorResponse) and reply.payload.get("said") is True

    # -- whole-objective writes -----------------------------------------

    def execute(self, action: Action) -> Result:
        if action.name == "navigate":
            outcome = self._client.navigate(dict(action.params.get("goal") or {}),
                                            timeout_ticks=_opt_int(action.params.get("timeout_ticks")),
                                            final_reach_distance=_opt_float(action.params.get("final_reach_distance")),
                                            survival_recovery=bool(action.params.get("survival_recovery", False)))
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
        elif action.name in _ACTION_TERMINALS:
            return self._execute_terminal_action(action)
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

    # -- lifecycle ------------------------------------------------------

    def spawn(
        self,
        pos=None,
        *,
        yaw=None,
        pitch=None,
        dimension=None,
        gamemode=None,
        emit_respawned=False,
        timeout_s=15.0,
    ) -> Result:
        reply = self._client.request_response(lambda protocol: protocol.spawn(
            self.bot_name,
            pos=None if pos is None else tuple(int(value) for value in pos),
            yaw=yaw,
            pitch=pitch,
            dimension=dimension,
            gamemode=gamemode,
            emit_respawned=bool(emit_respawned),
        ))
        if isinstance(reply, ErrorResponse):
            return _lifecycle_result(self.bot_name, "spawn", False, False, reply.payload, reply.code)
        accepted = bool(reply.payload.get("accepted"))
        if not accepted:
            return _lifecycle_result(
                self.bot_name, "spawn", False, False, reply.payload, "spawn_rejected"
            )
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        while time.monotonic() < deadline:
            state = self.get_state()
            if not state.missing:
                return _lifecycle_result(
                    self.bot_name,
                    "spawn",
                    True,
                    True,
                    {**dict(reply.payload), "final_pos": list(state.pos)},
                    None,
                )
            time.sleep(0.1)
        return _lifecycle_result(
            self.bot_name, "spawn", False, True, reply.payload, "spawn_timeout"
        )

    def despawn(self) -> Result:
        reply = self._client.request_response(
            lambda protocol: protocol.despawn(self.bot_name)
        )
        if isinstance(reply, ErrorResponse):
            return _lifecycle_result(self.bot_name, "despawn", False, False, reply.payload, reply.code)
        accepted = bool(reply.payload.get("accepted"))
        if not accepted:
            return _lifecycle_result(
                self.bot_name, "despawn", False, False, reply.payload, "despawn_rejected"
            )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.get_state().missing:
                return _lifecycle_result(
                    self.bot_name, "despawn", True, True, reply.payload, None
                )
            time.sleep(0.1)
        return _lifecycle_result(
            self.bot_name, "despawn", False, True, reply.payload, "despawn_timeout"
        )

    def await_action_terminal(self, action_id, timeout_s=15.0, poll_interval_s=0.10,
                              terminal_events=None, intermediate_events=None) -> Event:
        terminal = self._action_terminals.pop(str(action_id), None)
        if terminal is None:
            raise NotImplementedError(f"{_CAPABILITY_GAP}:await_action_terminal:{action_id}")
        if terminal_events is not None and terminal.name not in terminal_events:
            raise ValueError(f"unexpected Java terminal {terminal.name} for {action_id}")
        return terminal

    def ignite_block(self, pos, *, item=None, allow_server_substitute=False, timeout_s=8.0) -> Event:
        params: dict[str, object] = {
            "target": list(pos),
            "item": item or "minecraft:flint_and_steel",
            "allow_server_substitute": bool(allow_server_substitute),
            "timeout_ticks": max(1, min(200, int(timeout_s * 20.0 + 0.999))),
        }
        action = Action.create("igniteBlock", params)
        accepted = self.execute(action)
        if not accepted.ok or not accepted.accepted:
            return _rejected_terminal(action, self.bot_name, "igniteDone", accepted.error)
        return self.await_action_terminal(action.id, timeout_s=timeout_s)

    def sow_crop(self, pos, *, crop_block, seed_item=None, allow_server_substitute=False, timeout_s=8.0) -> Event:
        params: dict[str, object] = {
            "target": list(pos),
            "crop_block": crop_block,
            "seed_item": seed_item or "",
            "allow_server_substitute": bool(allow_server_substitute),
            "timeout_ticks": max(1, min(200, int(timeout_s * 20.0 + 0.999))),
        }
        action = Action.create("sowCrop", params)
        accepted = self.execute(action)
        if not accepted.ok or not accepted.accepted:
            return _rejected_terminal(action, self.bot_name, "sowDone", accepted.error)
        return self.await_action_terminal(action.id, timeout_s=timeout_s)

    def interrupt(self, reason: str | None = None) -> Result:
        reply = self._client.interrupt_body(reason)
        if isinstance(reply, ErrorResponse):
            return _lifecycle_result(self.bot_name, "interrupt", False, False, reply.payload, reply.code)
        accepted = bool(reply.payload.get("accepted"))
        return _lifecycle_result(
            self.bot_name,
            "interrupt",
            accepted,
            accepted,
            reply.payload,
            None if accepted else "interrupt_rejected",
            complete=bool(reply.payload.get("complete")),
        )

    def _execute_terminal_action(self, action: Action) -> Result:
        if action.name == "containerTransfer":
            outcome = self._client.container_transfer(action.id, dict(action.params))
        elif action.name == "craftItem":
            outcome = self._client.craft_item(action.id, dict(action.params))
        elif action.name == "engageEntity":
            outcome = self._client.engage_entity(action.id, dict(action.params))
        elif action.name == "followEntity":
            outcome = self._client.follow_entity(action.id, dict(action.params))
        elif action.name == "furnaceTransfer":
            outcome = self._client.furnace_transfer(action.id, dict(action.params))
        else:
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
            name=_ACTION_TERMINALS[action.name],
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


def _lifecycle_result(
    bot: str,
    action: str,
    ok: bool,
    accepted: bool,
    data: dict,
    error: str | None,
    *,
    complete: bool = True,
) -> Result:
    return Result(
        id=None,
        bot=bot,
        type="result",
        ok=ok,
        accepted=accepted,
        complete=complete,
        data={"action": action, **dict(data)},
        error=error,
    )


def _rejected_terminal(action: Action, bot: str, name: str, error: str | None) -> Event:
    reason = str(error or "body_rejected")
    return Event(
        seq=0,
        tick=0,
        bot=bot,
        name=name,
        data={
            "action_id": action.id,
            "success": False,
            "stopped_reason": reason,
            "reason": reason,
        },
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
