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
from minebot.contract import Action
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
    RealClientCapability("baritone_settings", "settings", "adapter", "Bounded settings mutation through the client seam"),
    RealClientCapability("explore_filter", "explore_filter", "adapter", "JSON filter application through the client seam"),
    RealClientCapability("builder_controls", "builder", "adapter", "Pause, resume, and open schematic/litematic controls"),
    RealClientCapability("builder_file", "builder", "adapter", "Client-local schematic or litematic file entry point"),
    RealClientCapability("elytra_controls", "elytra", "adapter", "Repack and reset controls; flight still needs a fixture"),
    RealClientCapability("get_to_block_controls", "get_to_block", "adapter", "Blacklist-closest policy toggle"),
    RealClientCapability("builder_inspection", "builder", "adapter", "Placeable-block and layer inspection"),
    RealClientCapability("builder_selections", "selection", "adapter", "Builder selection add, clear, and geometry transforms"),
    RealClientCapability("body_primitives", "minecraft_client", "adapter", "Native jump, look, use, combat attempt, inventory, block and crop actions"),
    RealClientCapability("world_reads", "minecraft_client", "adapter", "Authoritative client-world block and entity snapshots"),
)
CAPABILITY_REGISTRY: Mapping[str, RealClientCapability] = {item.name: item for item in _CAPABILITIES}

UNSUPPORTED_CAPABILITIES: Mapping[str, str] = {
    "engageEntity": "Baritone exposes no combat process; a one-shot client attack cannot satisfy approach, cooldown, safety, and kill verification.",
    "rangedAttack": "Baritone exposes no ranged-combat process; charge, projectile trajectory, and hit verification need a separate client combat implementation.",
    "craftItem": "Baritone has no inventory crafting transaction API and the client seam does not own recipe-screen lifecycle or authoritative output verification.",
    "containerTransfer": "Baritone has no container transaction API; reliable transfer needs server menu lifecycle, slot mapping, and reopen/reconciliation semantics.",
    "furnaceTransfer": "Baritone has no furnace transaction API; reliable transfer needs server menu lifecycle and slot-state verification.",
    "handoffItem": "Baritone/client IPC has no recipient identity and delivery receipt protocol for another player.",
    "ascend": "The Body contract requires verified surface recovery; a Baritone Y goal does not prove a safe surface or governed vertical mutation.",
    "digDown": "The Body contract requires bounded, governed shaft excavation and fall/liquid checks; a generic Baritone goal cannot provide those guarantees.",
    "digUp": "The Body contract requires bounded, governed pillar construction and clearance checks; a generic Baritone goal cannot provide those guarantees.",
    "navigationMutationDecision": "This is the frozen server-governance proposal action; the real client route has no equivalent server proposal owner.",
    "baritoneCommand": "The generic Baritone command manager has no typed Body result semantics; expose individual commands only after their result contract is adapted.",
    "spawn": "The real client process is owned by its launcher; Body does not create or destroy a Minecraft client process.",
    "despawn": "The real client process is owned by its launcher; Body does not terminate a Minecraft client process.",
}


@dataclass
class _PendingAction:
    action: Action
    done: threading.Event
    terminal: Event | None = None


class RealClientBody:
    """Body-contract adapter backed by one real Minecraft client process.

    The IPC command is executed by a worker so ``interrupt`` can send a
    cancellation while Baritone is still pathing.  The IPC transport owns one
    correlated reader, so state and world reads remain safe while a physical
    action is active.
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

    def unsupported_capability_manifest(self) -> list[dict[str, str]]:
        return [
            {"action": action, "reason": reason}
            for action, reason in sorted(UNSUPPORTED_CAPABILITIES.items())
        ]

    def _ensure_connected(self) -> None:
        self.ipc.connect()

    def world_identity(self) -> str:
        """Return the client dimension as the session's stable world scope.

        A launcher may provide a stronger world id through the session config;
        the dimension fallback keeps the shared runtime namespace deterministic
        without inventing a server-side seed or pretending the client owns it.
        """
        state = self._state_payload()
        return f"real-client:{state.get('dimension') or 'unknown'}"

    def get_state(self) -> BodyState:
        cached = self._state_payload()
        with self._lock:
            active = self._active is not None
        return _body_state(self.bot_name, cached, pending=active)

    def _state_payload(self) -> dict[str, Any]:
        self._ensure_connected()
        cached = self.ipc.state()
        with self._lock:
            self._last_state_payload = dict(cached)
        return cached

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        if scope == "inventory":
            return self._inventory_perception(params)
        if scope == "findBlocks":
            return self._find_blocks_perception(params)
        if scope in {"blockAt", "blockCells", "nearbyBlocks"}:
            return self._world_read_perception(scope, params)
        if scope in {"baritoneState", "processState"}:
            try:
                state = self._state_payload()
            except Exception as error:
                return _gap_perception(self.bot_name, scope, f"state_read:{type(error).__name__}")
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={
                    key: state[key]
                    for key in (
                        "pathing", "any_process_active", "mine_active", "goal_active",
                        "get_active", "explore_active", "follow_active", "farm_active",
                        "builder_active", "builder_paused", "elytra_active", "elytra_loaded",
                        "elytra_safe_to_cancel", "elytra_destination", "elytra_path_size",
                        "baritone_goal", "following", "allow_break", "allow_place",
                        "explore_for_blocks", "replant_crops", "farm_max_scan_size",
                        "get_to_block_blacklist", "builder_placeable", "builder_min_layer",
                        "builder_max_layer", "builder_selections", "path_estimated_ticks",
                    )
                    if key in state
                },
                uncertainty=[],
            )
        if scope in {"nearbyEntities", "nearbyHostiles"}:
            return self._entity_perception(scope, params)
        if scope == "capabilities":
            return PerceptionResult(
                bot=self.bot_name,
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={
                    "capabilities": self.capability_manifest(),
                    "unsupported": self.unsupported_capability_manifest(),
                },
                uncertainty=[],
            )
        return _gap_perception(self.bot_name, scope)

    def _entity_perception(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        try:
            state = self._state_payload()
        except Exception as error:
            return _gap_perception(self.bot_name, scope, f"state_read:{type(error).__name__}")
        raw_entities = state.get("entities")
        if not isinstance(raw_entities, list):
            return _gap_perception(self.bot_name, scope, "client_entity_snapshot_missing")
        limit = max(1, min(128, int(params.get("limit") or 64)))
        entities = [dict(item) for item in raw_entities if isinstance(item, Mapping)]
        if scope == "nearbyHostiles":
            entities = [item for item in entities if bool(item.get("hostile"))]
        return PerceptionResult(
            bot=self.bot_name,
            scope=scope,
            type="perception",
            ok=True,
            complete=True,
            data={"entities": entities[:limit], "count": min(len(entities), limit)},
            uncertainty=[],
        )

    def _world_read_perception(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        try:
            self._ensure_connected()
            response = self.ipc.world_read(scope, params)
        except Exception as error:
            return _gap_perception(self.bot_name, scope, f"world_read:{type(error).__name__}")
        data = response.get("data")
        if not isinstance(data, dict):
            data = {key: value for key, value in response.items() if key not in {"type", "request_id"}}
        return PerceptionResult(
            bot=self.bot_name,
            scope=scope,
            type="perception",
            ok=bool(response.get("ok", True)),
            complete=bool(response.get("complete", True)),
            data=dict(data),
            uncertainty=list(response.get("uncertainty") or []),
            error=None if response.get("ok", True) else str(response.get("error") or "world_read_failed"),
        )

    def execute(self, action: Action) -> Result:
        objective = _objective_for(action)
        control = None if objective is not None else _control_for(action)
        if objective is None and control is None:
            return _gap_result(action, self.bot_name, "unsupported_action")
        with self._lock:
            if self._active is not None and (
                control is None or control[0] not in {"builder_pause", "builder_resume"}
            ):
                return _gap_result(action, self.bot_name, "body_busy")
            pending = _PendingAction(action=action, done=threading.Event())
            if control is None:
                self._active = pending
            self._pending[action.id] = pending
        thread = threading.Thread(
            target=self._run_action,
            args=(pending, objective, control, _timeout_ms(action)),
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
            data={
                "action": action.name,
                "objective": objective,
                "control": None if control is None else control[0],
                "provider": "baritone",
            },
        )

    def _run_action(
        self,
        pending: _PendingAction,
        objective: str | None,
        control: tuple[str, dict[str, object]] | None,
        timeout_ms: int,
    ) -> None:
        try:
            self._ensure_connected()
            if control is not None:
                control_type, fields = control
                response = self.ipc.control(control_type, **fields)
                event = _control_event(self.bot_name, pending.action, response)
                self._merge_state_payload(response)
            else:
                assert objective is not None
                terminal = self.ipc.command(objective, action_id=pending.action.id, timeout_ms=timeout_ms)
                event = _terminal_event(self.bot_name, pending.action, terminal)
                self._merge_state_payload(terminal.payload)
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

    def _merge_state_payload(self, payload: Mapping[str, object]) -> None:
        if "position" in payload:
            with self._lock:
                self._last_state_payload = {**(self._last_state_payload or {}), **payload}

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
        if not args:
            return _gap_event(self.bot_name, "igniteDone", "invalid_request:missing_target")
        pos = args[0]
        params: dict[str, object] = {"target": list(pos) if isinstance(pos, (list, tuple)) else pos}
        params.update(kwargs)
        return self._execute_sync_event(Action.create("igniteBlock", params), "igniteDone")

    def sow_crop(self, *args: object, **kwargs: object) -> Event:
        if not args:
            return _gap_event(self.bot_name, "sowDone", "invalid_request:missing_target")
        pos = args[0]
        params: dict[str, object] = {"target": list(pos) if isinstance(pos, (list, tuple)) else pos}
        params.update(kwargs)
        return self._execute_sync_event(Action.create("sowCrop", params), "sowDone")

    def _execute_sync_event(self, action: Action, expected_name: str) -> Event:
        accepted = self.execute(action)
        if not accepted.ok or not accepted.accepted:
            return _gap_event(self.bot_name, expected_name, accepted.error or "body_rejected")
        try:
            return self.await_action_terminal(action.id, timeout_s=_timeout_seconds(action))
        except Exception as error:
            return _gap_event(self.bot_name, expected_name, f"terminal:{type(error).__name__}")

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
        yaw=_optional_float(payload.get("yaw")),
        pitch=_optional_float(payload.get("pitch")),
        health=float(payload.get("health") or 0.0),
        food=int(payload.get("food") or 0),
        oxygen=_optional_int(payload.get("oxygen")),
        inventory_raw=raw_inventory,
        inventory_hash=hashlib.sha256(raw_inventory.encode()).hexdigest(),
        effects=None,
        time=0,
        weather=None,
        dimension=str(payload.get("dimension") or "minecraft:overworld"),
        complete=True,
        missing=False,
        inventory_counts=inventory,
        selected_slot=_optional_int(payload.get("selected_slot")),
        selected_item=_optional_item(payload.get("selected_item")),
        offhand_item=_optional_item(payload.get("offhand_item")),
        body_owner=bot_name,
        pending_action_count=1 if pending else 0,
    )


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _optional_item(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _unnamespace(value)


def _objective_for(action: Action) -> str | None:
    params = dict(action.params)
    name = action.name
    if name == "navigate":
        return _goal_objective(params.get("goal"), params.get("final_reach_distance"))
    if name in {"moveTo", "navigateTo"}:
        target = _position(params.get("target"))
        if target is None:
            waypoints = params.get("waypoints")
            if isinstance(waypoints, (list, tuple)) and waypoints:
                target = _position(waypoints[-1])
        if target is None:
            return None
        return f"near:{target[0]}:{target[1]}:{target[2]}:{max(1, math.ceil(float(params.get('arrival_radius', 1))))}"
    if name in {"mineResource", "baritoneMine"}:
        return _mine_objective(params)
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
    if name == "rangedAttack":
        return None
    return None


def _control_for(action: Action) -> tuple[str, dict[str, object]] | None:
    params = dict(action.params)
    name = action.name
    if name in {"baritoneSettings", "setBaritoneSettings"}:
        values = params.get("settings")
        if not isinstance(values, Mapping):
            values = {
                key: value
                for key, value in params.items()
                if key in {"allow_break", "allow_place", "explore_for_blocks", "replant_crops", "farm_max_scan_size"}
            }
        return ("settings", dict(values)) if values else None
    if name in {"builderPause", "baritoneBuilderPause"}:
        return ("builder_pause", {})
    if name in {"builderResume", "baritoneBuilderResume"}:
        return ("builder_resume", {})
    if name in {"builderOpenSchematic", "baritoneBuilderOpenSchematic"}:
        return ("builder_open_schematic", {})
    if name in {"builderOpenLitematic", "baritoneBuilderOpenLitematic"}:
        try:
            index = int(params.get("index") or 0)
        except (TypeError, ValueError):
            return None
        return ("builder_open_litematic", {"index": index})
    if name in {"builderBuildFile", "baritoneBuilderBuildFile"}:
        path = params.get("path")
        origin = params.get("origin") or params.get("anchor")
        if not isinstance(path, str) or not path.strip() or not isinstance(origin, (list, tuple)):
            return None
        name_value = params.get("name") or path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if not isinstance(name_value, str) or not name_value.strip():
            return None
        return ("builder_file", {"path": path, "name": name_value, "origin": list(origin)})
    if name in {"exploreFilter", "baritoneExploreFilter"}:
        path = params.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        return ("explore_filter", {"path": path, "invert": bool(params.get("invert", False))})
    if name in {"elytraRepack", "baritoneElytraRepack"}:
        return ("elytra_repack", {})
    if name in {"elytraReset", "baritoneElytraReset"}:
        return ("elytra_reset", {})
    if name in {"getToBlockBlacklistClosest", "baritoneGetToBlockBlacklistClosest"}:
        return ("get_to_block_blacklist", {})
    if name in {"builderSelectionAdd", "baritoneBuilderSelectionAdd"}:
        pos1 = params.get("pos1")
        pos2 = params.get("pos2")
        if not isinstance(pos1, (list, tuple)) or not isinstance(pos2, (list, tuple)):
            return None
        return ("builder_selection", {"operation": "add", "pos1": list(pos1), "pos2": list(pos2)})
    if name in {"builderSelectionClear", "baritoneBuilderSelectionClear"}:
        return ("builder_selection", {"operation": "clear"})
    if name in {
        "builderSelectionExpand", "baritoneBuilderSelectionExpand",
        "builderSelectionContract", "baritoneBuilderSelectionContract",
        "builderSelectionShift", "baritoneBuilderSelectionShift",
    }:
        direction = params.get("direction")
        if not isinstance(direction, str) or not direction.strip():
            return None
        try:
            amount = int(params.get("amount", 1))
        except (TypeError, ValueError):
            return None
        if amount < 1 or amount > 256:
            return None
        operation = "expand" if "Expand" in name else "contract" if "Contract" in name else "shift"
        return ("builder_selection", {"operation": operation, "direction": direction, "amount": amount})
    if name in {
        "jump", "stop", "selectItem", "selectSlot", "lookAt", "useItem", "attackEntity",
        "dropItem", "moveItem", "mineBlock", "collectBlock", "placeBlock",
        "igniteBlock", "sowCrop",
    }:
        return ("primitive", {"action_name": name, "params": params})
    return None


def _goal_objective(raw_goal: object, arrival_radius: object) -> str | None:
    if not isinstance(raw_goal, Mapping):
        return None
    kind = str(raw_goal.get("kind") or "")
    if kind == "near":
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
    if kind == "block":
        position = _position(
            raw_goal.get("pos")
            if raw_goal.get("pos") is not None
            else [raw_goal.get("x"), raw_goal.get("y"), raw_goal.get("z")]
        )
        if position is None:
            return None
        return f"block:{position[0]}:{position[1]}:{position[2]}"
    if kind == "xz":
        try:
            return f"xz:{int(raw_goal['x'])}:{int(raw_goal['z'])}"
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "y_level":
        try:
            return f"y:{int(raw_goal['y'])}"
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "avoid":
        fallback = raw_goal.get("fallback")
        if fallback is not None:
            return None
        pos = _position(raw_goal.get("pos"))
        if pos is None:
            return None
        try:
            distance = max(1, int(raw_goal.get("min_distance", 1)))
        except (TypeError, ValueError):
            return None
        return f"away:{pos[0]}:{pos[1]}:{pos[2]}:{distance}"
    if kind == "composite":
        if str(raw_goal.get("mode") or "any") != "any":
            return None
        children = raw_goal.get("goals")
        if not isinstance(children, (list, tuple)) or not 1 <= len(children) <= 64:
            return None
        tokens = [_goal_token(child) for child in children]
        if any(token is None for token in tokens):
            return None
        return "any:" + "|".join(token for token in tokens if token is not None)
    return None


def _goal_token(raw_goal: object) -> str | None:
    if not isinstance(raw_goal, Mapping):
        return None
    kind = str(raw_goal.get("kind") or "")
    if kind == "near":
        position = _position(
            raw_goal.get("pos")
            if raw_goal.get("pos") is not None
            else [raw_goal.get("x"), raw_goal.get("y"), raw_goal.get("z")]
        )
        if position is None:
            return None
        try:
            radius = max(1, math.ceil(float(raw_goal.get("range", raw_goal.get("radius", 1)))))
        except (TypeError, ValueError):
            return None
        return f"near:{position[0]}:{position[1]}:{position[2]}:{radius}"
    if kind == "xz":
        try:
            return f"xz:{int(raw_goal['x'])}:{int(raw_goal['z'])}"
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "y_level":
        try:
            return f"y:{int(raw_goal['y'])}"
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "block":
        position = _position(
            raw_goal.get("pos")
            if raw_goal.get("pos") is not None
            else [raw_goal.get("x"), raw_goal.get("y"), raw_goal.get("z")]
        )
        return None if position is None else f"block:{position[0]}:{position[1]}:{position[2]}"
    return None


def _mine_objective(params: Mapping[str, object]) -> str | None:
    raw_blocks = params.get("blocks")
    if isinstance(raw_blocks, str):
        blocks = [raw_blocks]
    elif isinstance(raw_blocks, (list, tuple)):
        blocks = [str(value) for value in raw_blocks]
    else:
        target = params.get("block") or params.get("target")
        blocks = [target] if isinstance(target, str) else []
    normalized = [_unnamespace(value) for value in blocks if isinstance(value, str) and value.strip()]
    if not normalized:
        return None
    item = params.get("item")
    expected = _unnamespace(item) if isinstance(item, str) and item.strip() else normalized[0]
    quantity_raw = params.get("quantity", params.get("count", 1))
    try:
        quantity = max(1, min(4096, int(quantity_raw)))
    except (TypeError, ValueError):
        return None
    if len(normalized) == 1 and quantity == 1 and expected == normalized[0]:
        return f"mine:{normalized[0]}"
    if len(normalized) == 1 and expected != normalized[0] and quantity == 1:
        return f"mine:{normalized[0]}:{expected}"
    return f"mine:{quantity}:{','.join(normalized)}:{expected}"


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


def _timeout_seconds(action: Action) -> float:
    return _timeout_ms(action) / 1000.0 + 5.0


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


def _control_event(bot_name: str, action: Action, response: Mapping[str, object]) -> Event:
    data = dict(response)
    data.update({
        "action_id": action.id,
        "success": True,
        "status": "completed",
        "reason": "control_applied",
    })
    return Event(seq=0, tick=0, bot=bot_name, name=_terminal_name(action.name), data=data)


def _terminal_name(action_name: str) -> str:
    return {
        "navigate": "navigateDone",
        "moveTo": "moveDone",
        "navigateTo": "navigateDone",
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
        "baritoneSettings": "baritoneSettingsDone",
        "setBaritoneSettings": "baritoneSettingsDone",
        "builderPause": "builderPauseDone",
        "baritoneBuilderPause": "builderPauseDone",
        "builderResume": "builderResumeDone",
        "baritoneBuilderResume": "builderResumeDone",
        "builderOpenSchematic": "builderOpenSchematicDone",
        "baritoneBuilderOpenSchematic": "builderOpenSchematicDone",
        "builderOpenLitematic": "builderOpenLitematicDone",
        "baritoneBuilderOpenLitematic": "builderOpenLitematicDone",
        "exploreFilter": "exploreFilterDone",
        "baritoneExploreFilter": "exploreFilterDone",
        "elytraRepack": "elytraRepackDone",
        "baritoneElytraRepack": "elytraRepackDone",
        "elytraReset": "elytraResetDone",
        "baritoneElytraReset": "elytraResetDone",
        "getToBlockBlacklistClosest": "getToBlockBlacklistDone",
        "baritoneGetToBlockBlacklistClosest": "getToBlockBlacklistDone",
        "builderSelectionAdd": "builderSelectionDone",
        "baritoneBuilderSelectionAdd": "builderSelectionDone",
        "builderSelectionClear": "builderSelectionDone",
        "baritoneBuilderSelectionClear": "builderSelectionDone",
        "builderSelectionExpand": "builderSelectionDone",
        "baritoneBuilderSelectionExpand": "builderSelectionDone",
        "builderSelectionContract": "builderSelectionDone",
        "baritoneBuilderSelectionContract": "builderSelectionDone",
        "builderSelectionShift": "builderSelectionDone",
        "baritoneBuilderSelectionShift": "builderSelectionDone",
        "builderBuildFile": "builderFileDone",
        "baritoneBuilderBuildFile": "builderFileDone",
        "baritoneCommand": "baritoneCommandDone",
        "executeBaritoneCommand": "baritoneCommandDone",
        "jump": "jumpDone",
        "stop": "stopDone",
        "selectItem": "selectItemDone",
        "selectSlot": "selectSlotDone",
        "lookAt": "lookDone",
        "useItem": "useDone",
        "attackEntity": "attackDone",
        "dropItem": "dropDone",
        "moveItem": "moveItemDone",
        "mineBlock": "mineDone",
        "collectBlock": "mineDone",
        "placeBlock": "placeDone",
        "igniteBlock": "igniteDone",
        "sowCrop": "sowDone",
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


__all__ = [
    "CAPABILITY_REGISTRY",
    "UNSUPPORTED_CAPABILITIES",
    "RealClientBody",
    "RealClientCapability",
]
