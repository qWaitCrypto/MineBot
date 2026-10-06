"""Abstract Body adapter for the validated real-client + Baritone tuple.

This module is the first production-facing seam above the disposable client
probe.  It deliberately maps semantic Body actions to bounded upstream
Baritone processes; it does not expose Baritone internals as model tools and
it never falls back to the server FakePlayer implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time
from typing import Any, Mapping

from minebot.contract import BodyState, Event, PerceptionResult, Result
from minebot.contract import Action, Body
from minebot.game.errors import BodyActionTimeoutError
from minebot.game.real_client_ipc import RealClientIpc, RealClientIpcError, RealClientTerminal


_CAPABILITY_GAP = "capability_unavailable"


@dataclass(frozen=True)
class RealClientCapability:
    """One semantic capability exposed by this Body adapter."""

    name: str
    process: str
    status: str
    note: str


_CAPABILITIES: tuple[RealClientCapability, ...] = (
    RealClientCapability("navigate", "custom_goal", "verified", "GoalBlock, GoalXZ, and GoalNear"),
    RealClientCapability("mine_resource", "mine", "verified", "Inventory-delta verified resource mining"),
    RealClientCapability("get_to_block", "get_to_block", "verified", "Reach and interaction state are observed"),
    RealClientCapability("explore", "explore", "callable", "Long-lived process with cancellation; no finite completion"),
    RealClientCapability("follow_entity", "follow", "verified", "Named/type target approach and cancellation"),
    RealClientCapability("pickup_items", "follow_pickup", "verified", "Dropped-item pickup with inventory delta"),
    RealClientCapability("farm_area", "farm", "verified", "Mature crop harvest in the validated fixture"),
    RealClientCapability("build_structure", "builder", "verified", "Fill schematic with pause/resume"),
    RealClientCapability("clear_area", "builder_clear", "verified", "Clear-area world verification"),
    RealClientCapability("elytra_travel", "elytra", "callable", "Process start/cancel only; flight needs a separate fixture"),
)
CAPABILITY_REGISTRY: Mapping[str, RealClientCapability] = {item.name: item for item in _CAPABILITIES}


@dataclass
class _PendingAction:
    action: Action
    done: threading.Event
    terminal: Event | None = None


class RealClientBody:
    """Body-contract adapter backed by one real Minecraft client process.

    The IPC command is executed by a worker so ``interrupt`` can send a
    cancellation while Baritone is still pathing.  Only the worker reads
    incoming frames from the single loopback connection; read methods return a
    cached state while a physical action is active to avoid interleaving JSON
    replies on that connection.
    """

    def __init__(self, bot_name: str, ipc: RealClientIpc) -> None:
        self.bot_name = bot_name
        self.ipc = ipc
        self.last_seq = 0
        self.event_log: list[Event] = []
        self._lock = threading.RLock()
        self._active: _PendingAction | None = None
        self._pending: dict[str, _PendingAction] = {}
        self._event_queue: list[Event] = []
        self._last_state_payload: dict[str, Any] | None = None

    @property
    def capabilities(self) -> Mapping[str, RealClientCapability]:
        return CAPABILITY_REGISTRY

    def capability_manifest(self) -> list[dict[str, str]]:
        return [
            {
                "name": capability.name,
                "process": capability.process,
                "status": capability.status,
                "note": capability.note,
            }
            for capability in _CAPABILITIES
        ]

    def _ensure_connected(self) -> None:
        self.ipc.connect()

    def get_state(self) -> BodyState:
        with self._lock:
            active = self._active is not None
            cached = dict(self._last_state_payload or {})
        if active and not cached:
            raise RuntimeError("state_unavailable_while_action_active")
        if not active:
            self._ensure_connected()
            cached = self.ipc.state()
            with self._lock:
                self._last_state_payload = dict(cached)
        return _body_state(self.bot_name, cached, pending=active)

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        if scope == "inventory":
            return self._inventory_perception(params)
        if scope == "findBlocks":
            return self._find_blocks_perception(params)
        if scope == "capabilities":
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={"capabilities": self.capability_manifest()},
                uncertainty=[],
            )
        return _gap_perception(self.bot_name, scope)

    def execute(self, action: Action) -> Result:
        objective = _objective_for(action)
        if objective is None:
            return _gap_result(action, self.bot_name, "unsupported_action")
        with self._lock:
            if self._active is not None:
                return _gap_result(action, self.bot_name, "body_busy")
            pending = _PendingAction(action=action, done=threading.Event())
            self._active = pending
            self._pending[action.id] = pending
        thread = threading.Thread(
            target=self._run_action,
            args=(pending, objective, _timeout_ms(action)),
            name=f"minebot-real-client-{action.id[:8]}",
            daemon=True,
        )
        thread.start()
        return Result(
            id=action.id,
            bot=self.bot_name,
            type="result",
            ok=True,
            accepted=True,
            complete=False,
            data={"action": action.name, "objective": objective, "provider": "baritone"},
        )

    def _run_action(self, pending: _PendingAction, objective: str, timeout_ms: int) -> None:
        try:
            self._ensure_connected()
            terminal = self.ipc.command(objective, action_id=pending.action.id, timeout_ms=timeout_ms)
            event = _terminal_event(self.bot_name, pending.action, terminal)
            if isinstance(terminal.payload, dict):
                state = {
                    key: terminal.payload[key]
                    for key in (
                        "position", "health", "food", "dimension", "inventory",
                        "on_ground", "fall_flying", "chest_item",
                    )
                    if key in terminal.payload
                }
                if state:
                    with self._lock:
                        self._last_state_payload = {**(self._last_state_payload or {}), **state}
        except Exception as error:
            event = Event(
                seq=self._next_seq(),
                tick=0,
                bot=self.bot_name,
                name=_terminal_name(pending.action.name),
                data={
                    "action_id": pending.action.id,
                    "success": False,
                    "status": "failed",
                    "reason": f"transport:{type(error).__name__}",
                    "message": str(error),
                },
            )
        if event.seq == 0:
            event = Event(
                seq=self._next_seq(),
                tick=event.tick,
                bot=event.bot,
                name=event.name,
                data=dict(event.data),
            )
        with self._lock:
            pending.terminal = event
            pending.done.set()
            self._event_queue.append(event)
            self.event_log.append(event)
            if self._active is pending:
                self._active = None

    def await_action_terminal(
        self,
        action_id: str,
        timeout_s: float = 15.0,
        poll_interval_s: float = 0.10,
        terminal_events: set[str] | None = None,
        intermediate_events: set[str] | None = None,
    ) -> Event:
        del poll_interval_s, intermediate_events
        with self._lock:
            pending = self._pending.get(str(action_id))
        if pending is None:
            raise BodyActionTimeoutError(
                f"unknown real-client action: {action_id}",
                diagnostics={"action_id": action_id, "provider": "baritone"},
            )
        if not pending.done.wait(max(0.01, float(timeout_s))):
            raise BodyActionTimeoutError(
                f"real-client action timed out: {action_id}",
                diagnostics={"action_id": action_id, "provider": "baritone"},
            )
        assert pending.terminal is not None
        if terminal_events is not None and pending.terminal.name not in terminal_events:
            raise ValueError(f"unexpected real-client terminal {pending.terminal.name!r}")
        return pending.terminal

    def poll_events(self) -> list[Event]:
        with self._lock:
            events = list(self._event_queue)
            self._event_queue.clear()
        return events

    def interrupt(self, reason: str | None = None) -> Result:
        with self._lock:
            pending = self._active
        if pending is None:
            return Result(
                id=None,
                bot=self.bot_name,
                type="result",
                ok=False,
                accepted=False,
                complete=True,
                error="no_action_active",
            )
        try:
            self.ipc.cancel(pending.action.id)
        except RealClientIpcError as error:
            return Result(
                id=pending.action.id,
                bot=self.bot_name,
                type="result",
                ok=False,
                accepted=False,
                complete=True,
                error=f"cancel_transport:{type(error).__name__}",
            )
        return Result(
            id=pending.action.id,
            bot=self.bot_name,
            type="result",
            ok=True,
            accepted=True,
            complete=False,
            data={"action_id": pending.action.id, "reason": reason or "interrupt_requested"},
        )

    def spawn(self, *args: object, **kwargs: object) -> Result:
        del args, kwargs
        return _gap_result(None, self.bot_name, "client_lifecycle_owned_by_launcher")

    def despawn(self) -> Result:
        return _gap_result(None, self.bot_name, "client_lifecycle_owned_by_launcher")

    def ignite_block(self, *args: object, **kwargs: object) -> Event:
        del args, kwargs
        return _gap_event(self.bot_name, "igniteDone", "capability_unavailable:ignite_block")

    def sow_crop(self, *args: object, **kwargs: object) -> Event:
        del args, kwargs
        return _gap_event(self.bot_name, "sowDone", "capability_unavailable:sow_crop")

    def close(self) -> None:
        self.ipc.close()

    def _inventory_perception(self, params: dict[str, object]) -> PerceptionResult:
        try:
            state = self.get_state()
        except Exception as error:
            return _gap_perception(self.bot_name, "inventory", f"state_read:{type(error).__name__}")
        items = sorted((state.inventory_counts or {}).items())
        start = max(0, int(params.get("start") or 0))
        limit = max(1, min(46, int(params.get("limit") or 46)))
        page = items[start : start + limit]
        slots = [
            {
                "slot": start + index,
                "item": item,
                "count": int(count),
                "empty": False,
                "slotType": "player",
                "slotLabel": item,
            }
            for index, (item, count) in enumerate(page)
        ]
        next_start = start + limit if start + limit < len(items) else None
        return PerceptionResult(
            bot=self.bot_name,
            scope="inventory",
            type="perception",
            ok=True,
            complete=next_start is None,
            data={
                "start": start,
                "limit": limit,
                "nextStart": next_start,
                "totalSlots": len(items),
                "slots": slots,
            },
            uncertainty=[] if next_start is None else [{"reason": "page_limit"}],
            next=None if next_start is None else str(next_start),
        )

    def _find_blocks_perception(self, params: dict[str, object]) -> PerceptionResult:
        raw_types = params.get("block_types") or params.get("types") or params.get("block_ids")
        if isinstance(raw_types, str):
            targets = [raw_types]
        elif isinstance(raw_types, (list, tuple)):
            targets = [str(value) for value in raw_types]
        else:
            target = params.get("block")
            targets = [str(target)] if target else []
        if not targets:
            return _gap_perception(self.bot_name, "findBlocks", "invalid_request:no_block_types")
        with self._lock:
            if self._active is not None:
                return _gap_perception(self.bot_name, "findBlocks", "body_busy")
        try:
            self._ensure_connected()
            locations: list[dict[str, object]] = []
            for target in targets[:16]:
                response = self.ipc.scan(_unnamespace(target))
                for raw in response.get("locations") or []:
                    if isinstance(raw, (list, tuple)) and len(raw) == 3:
                        locations.append({"x": int(raw[0]), "y": int(raw[1]), "z": int(raw[2]), "type": _unnamespace(target)})
        except Exception as error:
            return _gap_perception(self.bot_name, "findBlocks", f"scan:{type(error).__name__}")
        limit = max(1, min(256, int(params.get("limit") or 32)))
        matches = locations[:limit]
        return PerceptionResult(
            bot=self.bot_name,
            scope="findBlocks",
            type="perception",
            ok=True,
            complete=True,
            data={
                "start": 0,
                "limit": limit,
                "count": len(matches),
                "totalMatches": len(locations),
                "nextStart": None,
                "matches": matches,
                "coverage_complete": True,
            },
            uncertainty=[],
        )

    def _next_seq(self) -> int:
        with self._lock:
            self.last_seq += 1
            return self.last_seq


def _body_state(bot_name: str, payload: Mapping[str, Any], *, pending: bool) -> BodyState:
    position = payload.get("position")
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        return BodyState(
            bot=bot_name,
            pos=(0.0, 0.0, 0.0),
            yaw=None,
            pitch=None,
            health=0.0,
            food=0,
            oxygen=None,
            inventory_raw="",
            inventory_hash="",
            effects=None,
            time=0,
            weather=None,
            dimension=None,
            complete=False,
            missing=True,
            pending_action_count=1 if pending else 0,
        )
    inventory = {
        _unnamespace(str(item)): int(count or 0)
        for item, count in dict(payload.get("inventory") or {}).items()
    }
    raw_inventory = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
    return BodyState(
        bot=bot_name,
        pos=(float(position[0]), float(position[1]), float(position[2])),
        yaw=None,
        pitch=None,
        health=float(payload.get("health") or 0.0),
        food=int(payload.get("food") or 0),
        oxygen=None,
        inventory_raw=raw_inventory,
        inventory_hash=hashlib.sha256(raw_inventory.encode()).hexdigest(),
        effects=None,
        time=0,
        weather=None,
        dimension=str(payload.get("dimension") or "minecraft:overworld"),
        complete=True,
        missing=False,
        inventory_counts=inventory,
        selected_item=None,
        offhand_item=None,
        body_owner=bot_name,
        pending_action_count=1 if pending else 0,
    )


def _objective_for(action: Action) -> str | None:
    params = dict(action.params)
    name = action.name
    if name == "navigate":
        return _goal_objective(params.get("goal"), params.get("final_reach_distance"))
    if name in {"mineResource", "baritoneMine"}:
        return _item_objective("mine", params.get("block") or params.get("target"), params.get("item"))
    if name in {"getToBlock", "baritoneGetToBlock"}:
        return _item_objective("get", params.get("block") or params.get("target"))
    if name in {"explore", "baritoneExplore"}:
        anchor = _position(params.get("anchor") or params.get("pos"))
        return None if anchor is None else f"explore:{anchor[0]}:{anchor[2]}"
    if name in {"followEntity", "baritoneFollow"}:
        target = params.get("target") or params.get("target_name") or params.get("target_type")
        return None if not isinstance(target, str) or not target.strip() else f"follow:{target.strip()}"
    if name in {"pickupItems", "baritonePickup"}:
        return _item_objective("pickup", params.get("item") or params.get("target"))
    if name in {"farmArea", "baritoneFarm"}:
        anchor = _position(params.get("anchor") or params.get("pos"))
        if anchor is None:
            return None
        return f"farm:{max(1, int(params.get('range') or 16))}:{anchor[0]}:{anchor[1]}:{anchor[2]}"
    if name in {"buildStructure", "baritoneBuild"}:
        anchor = _position(params.get("anchor") or params.get("origin"))
        size = params.get("size")
        if anchor is None or not isinstance(size, (list, tuple)) or len(size) != 3:
            return None
        block = params.get("block") or params.get("target")
        if not isinstance(block, str) or not block.strip():
            return None
        return f"build:{_unnamespace(block)}:{anchor[0]}:{anchor[1]}:{anchor[2]}:{int(size[0])}:{int(size[1])}:{int(size[2])}"
    if name in {"clearArea", "baritoneClear"}:
        start = _position(params.get("start") or params.get("anchor"))
        end = _position(params.get("end"))
        if start is None or end is None:
            return None
        return f"clear:{start[0]}:{start[1]}:{start[2]}:{end[0]}:{end[1]}:{end[2]}"
    if name in {"elytraTravel", "baritoneElytra"}:
        target = _position(params.get("target") or params.get("destination"))
        return None if target is None else f"elytra:{target[0]}:{target[1]}:{target[2]}"
    return None


def _goal_objective(raw_goal: object, arrival_radius: object) -> str | None:
    if not isinstance(raw_goal, Mapping):
        return None
    kind = str(raw_goal.get("kind") or "")
    if kind in {"near", "block"}:
        pos = raw_goal.get("pos")
        if pos is None:
            pos = [raw_goal.get("x"), raw_goal.get("y"), raw_goal.get("z")]
        position = _position(pos)
        if position is None:
            return None
        radius_raw = raw_goal.get("range", raw_goal.get("radius", arrival_radius or 1))
        try:
            radius = max(1, math.ceil(float(radius_raw)))
        except (TypeError, ValueError):
            return None
        return f"near:{position[0]}:{position[1]}:{position[2]}:{radius}"
    if kind == "xz":
        try:
            return f"xz:{int(raw_goal['x'])}:{int(raw_goal['z'])}"
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _item_objective(kind: str, value: object, item: object = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    target = _unnamespace(value)
    if item is None:
        return f"{kind}:{target}"
    if not isinstance(item, str) or not item.strip():
        return None
    return f"{kind}:{target}:{_unnamespace(item)}"


def _position(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        return tuple(int(part) for part in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _timeout_ms(action: Action) -> int:
    raw = action.params.get("timeout_ms")
    if raw is None and action.params.get("timeout_s") is not None:
        raw = float(action.params["timeout_s"]) * 1000
    try:
        return max(1_000, min(300_000, int(raw or 60_000)))
    except (TypeError, ValueError):
        return 60_000


def _terminal_event(bot_name: str, action: Action, terminal: RealClientTerminal) -> Event:
    payload = dict(terminal.payload)
    position = payload.get("position")
    if isinstance(position, (list, tuple)) and len(position) == 3:
        payload.setdefault("final_x", position[0])
        payload.setdefault("final_y", position[1])
        payload.setdefault("final_z", position[2])
    payload.update({
        "action_id": action.id,
        "success": terminal.completed,
        "status": terminal.status,
        "reason": terminal.reason,
        "objective": payload.get("objective"),
    })
    return Event(
        seq=0,
        tick=0,
        bot=bot_name,
        name=_terminal_name(action.name),
        data=payload,
    )


def _terminal_name(action_name: str) -> str:
    return {
        "navigate": "navigateDone",
        "mineResource": "mineDone",
        "baritoneMine": "mineDone",
        "getToBlock": "getToBlockDone",
        "baritoneGetToBlock": "getToBlockDone",
        "explore": "exploreDone",
        "baritoneExplore": "exploreDone",
        "followEntity": "followDone",
        "baritoneFollow": "followDone",
        "pickupItems": "pickupDone",
        "baritonePickup": "pickupDone",
        "farmArea": "farmDone",
        "baritoneFarm": "farmDone",
        "buildStructure": "buildDone",
        "baritoneBuild": "buildDone",
        "clearArea": "clearDone",
        "baritoneClear": "clearDone",
        "elytraTravel": "elytraDone",
        "baritoneElytra": "elytraDone",
    }.get(action_name, "actionDone")


def _gap_result(action: Action | None, bot_name: str, reason: str) -> Result:
    return Result(
        id=action.id if action is not None else None,
        bot=bot_name,
        type="result",
        ok=False,
        accepted=False,
        complete=True,
        error=f"{_CAPABILITY_GAP}:{reason}",
    )


def _gap_event(bot_name: str, name: str, reason: str) -> Event:
    return Event(seq=0, tick=0, bot=bot_name, name=name, data={"success": False, "reason": reason})


def _gap_perception(bot_name: str, scope: str, reason: str | None = None) -> PerceptionResult:
    return PerceptionResult(
        bot=bot_name,
        scope=scope,
        type="perception",
        ok=False,
        complete=True,
        error=f"{_CAPABILITY_GAP}:{reason or scope}",
        uncertainty=[{"reason": reason or "capability_unavailable"}],
    )


def _unnamespace(value: str) -> str:
    return str(value).strip().removeprefix("minecraft:")


__all__ = ["CAPABILITY_REGISTRY", "RealClientBody", "RealClientCapability"]
