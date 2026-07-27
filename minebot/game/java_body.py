"""JavaBody: the neutral ``Body``-contract face of the Java Body provider.

This is the abstraction seam the multi-provider design promises: the same
``minebot.contract.Body`` protocol that ScarpetBody implements, backed by the
``fakeplayer-body/1`` wire protocol. Reads are wire-native (BODY_STATE,
FIND_BLOCKS, the pushed event stream); physical objectives delegate whole
actions to the Java Body (navigate/collect); semantics the provider does not
offer return a **typed capability gap** — never a silent fallback to weaker
behavior, per the Body-layer capability-negotiation rule.

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


class JavaBody:
    """Neutral Body-contract adapter over :class:`JavaBodyClient`."""

    def __init__(self, client: JavaBodyClient, bot_name: str) -> None:
        self._client = client
        self.bot_name = bot_name

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
        if scope != "findBlocks":
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=False,
                complete=True,
                error=f"{_CAPABILITY_GAP}:{scope}",
            )
        reply = self._client.request_response(lambda p: p.find_blocks(
            self.bot_name,
            [str(item) for item in (params.get("block_ids") or ())],
            int(params.get("radius", 32)),
            vertical_radius=_opt_int(params.get("vertical_radius")),
            limit=_opt_int(params.get("limit")),
            cursor=params.get("cursor") if isinstance(params.get("cursor"), str) else None,
        ))
        if isinstance(reply, ErrorResponse):
            return PerceptionResult(
                bot=self.bot_name, scope=scope, type="perception",
                ok=False, complete=False, error=reply.code,
            )
        payload = reply.payload
        return PerceptionResult(
            bot=self.bot_name,
            scope=scope,
            type="perception",
            ok=True,
            complete=bool(payload.get("coverage_complete")) and payload.get("next_cursor") is None,
            data={
                "matches": payload.get("matches", []),
                "index_generation": payload.get("index_generation"),
                "unloaded_chunk_count": payload.get("unloaded_chunk_count"),
                "result_capped": payload.get("result_capped"),
            },
            next=payload.get("next_cursor"),
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
        raise NotImplementedError(f"{_CAPABILITY_GAP}:await_action_terminal")

    def ignite_block(self, pos, *, item=None, allow_server_substitute=False, timeout_s=8.0) -> Event:
        raise NotImplementedError(f"{_CAPABILITY_GAP}:ignite_block")

    def sow_crop(self, pos, *, crop_block, seed_item=None, allow_server_substitute=False, timeout_s=8.0) -> Event:
        raise NotImplementedError(f"{_CAPABILITY_GAP}:sow_crop")

    def interrupt(self, reason: str | None = None) -> Result:
        return _gap_result(Action.create("interrupt"), self.bot_name)


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
