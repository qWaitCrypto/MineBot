"""Latest execution snapshot of the Body operation in flight.

One small object per tool call (``BODY_EXECUTION_STATE.md``): what it is doing,
what last really advanced, and whether it still holds the Body. It is fed by
the native progress that already reaches the runtime and by the call boundary
in ``run_tool``. It observes; it is not a lock, a scheduler or a second state
cache, and it never decides anything.

A phase or a progress time is written only from evidence. Heartbeats, repeated
samples and a merely active process do not count as progress, and a phase the
samples cannot prove stays ``None``.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from minebot.panel.projection import PROCESS_FIELDS

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "unknown"})
# Displacement while pathing that counts as movement, in blocks (slice-1 rule).
MOVEMENT_PROGRESS_BLOCKS = 0.5
FACT_DELTA_LIMIT = 8

_PROCESS_PHASES = {
    "builder": "building",
    "mine": "mining",
    "get": "getting_block",
    "explore": "exploring",
    "follow": "following",
    "farm": "farming",
    "elytra": "flying",
    "goal": "moving",
}
_NAVIGATION_FACTS = ("dest", "calculating", "path_position", "repeated_failures", "last_failure")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _Operation:
    def __init__(self, operation_id: str, tool: str, arguments: Mapping[str, Any]) -> None:
        self.operation_id = operation_id
        self.tool = tool
        self.arguments = dict(arguments)
        self.action_id: str | None = None
        self.state = "accepted"
        self.phase: str | None = None
        self.reason: str | None = None
        self.control = "held"
        self.started = time.monotonic()
        self.observed_at: str | None = None
        self.last_progress_at: str | None = None
        self.last_progress = self.started
        self.progress_signal: str | None = None
        self.facts: dict[str, Any] = {}
        # Evidence baselines; a first sample sets them and proves no progress.
        self.position: tuple[float, float, float] | None = None
        self.inventory: dict[str, int] | None = None
        self.start_inventory: dict[str, int] | None = None
        self.max_matching: int | None = None
        self.path_highwater: dict[str, int] = {}
        self.world_edits = 0
        self.path_failed = 0
        self.path_failed_at_progress = 0


class ExecutionSnapshots:
    """Thread-safe holder of the current operation and the last finished one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: _Operation | None = None
        self._last: dict[str, Any] | None = None

    def begin(self, operation_id: str, tool: str, arguments: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            self._current = _Operation(operation_id, tool, arguments or {})

    def running(self, operation_id: str) -> None:
        with self._lock:
            op = self._owned(operation_id)
            if op is not None and op.state == "accepted":
                op.state = "running"

    def stopping(self) -> None:
        """A stop was requested; the operation holds the Body until it settles."""
        with self._lock:
            if self._current is not None and self._current.state not in TERMINAL_STATES:
                self._current.state = "stopping"

    def observe(self, operation_id: str, payload: Mapping[str, Any]) -> None:
        """Merge one native progress sample. Late samples of another call are ignored."""
        with self._lock:
            op = self._owned(operation_id)
            if op is None or op.state in TERMINAL_STATES:
                return
            if op.state == "accepted":
                op.state = "running"
            action_id = payload.get("action_id")
            if isinstance(action_id, str) and action_id:
                op.action_id = action_id
            op.observed_at = _utc_now()
            events = payload.get("path_events")
            failed = events.get("CALC_FAILED") if isinstance(events, Mapping) else None
            if isinstance(failed, int):
                if failed < op.path_failed:  # a new native action restarted the counter
                    op.path_failed_at_progress = 0
                op.path_failed = failed
            signal = self._progress_signal(op, payload)
            if signal is not None:
                op.last_progress = time.monotonic()
                op.last_progress_at = op.observed_at
                op.progress_signal = signal
                op.path_failed_at_progress = op.path_failed
            self._merge_facts(op, payload)

    def finish(self, operation_id: str, *, state: str, reason: str | None, control: str) -> None:
        with self._lock:
            op = self._owned(operation_id)
            if op is None:
                return
            op.state = state if state in TERMINAL_STATES else "unknown"
            op.reason = reason
            op.control = control
            op.action_id = None
            op.phase = None
            self._last = self._payload(op)
            self._current = None

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._current is None else self._payload(self._current)

    def last(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._last is None else json.loads(json.dumps(self._last))

    # -- internals -------------------------------------------------------------

    def _owned(self, operation_id: str) -> _Operation | None:
        op = self._current
        return op if op is not None and op.operation_id == operation_id else None

    @staticmethod
    def _progress_signal(op: _Operation, payload: Mapping[str, Any]) -> str | None:
        signal: str | None = None
        edits = payload.get("world_edits")
        if isinstance(edits, list):
            newest = max((int(edit.get("index", -1)) for edit in edits if isinstance(edit, Mapping)), default=-1) + 1
            if newest > op.world_edits:
                op.world_edits = newest
                signal = "world_edit"
        construction = payload.get("construction_progress")
        matching = construction.get("matching_cells") if isinstance(construction, Mapping) else None
        if isinstance(matching, int):
            if op.max_matching is not None and matching > op.max_matching:
                signal = "construction"
            op.max_matching = max(matching, op.max_matching or 0)
        navigation = payload.get("navigation") if isinstance(payload.get("navigation"), Mapping) else {}
        index = navigation.get("path_position")
        if isinstance(index, int):
            path = json.dumps([navigation.get("src"), navigation.get("dest"), navigation.get("path_movements")],
                              default=str)
            if path in op.path_highwater and index > op.path_highwater[path]:
                signal = signal or "path"
            op.path_highwater[path] = max(index, op.path_highwater.get(path, -1))
        position = payload.get("position")
        if isinstance(position, (list, tuple)) and len(position) == 3:
            current = tuple(float(value) for value in position)
            moved = op.position is not None and sum(
                (a - b) ** 2 for a, b in zip(current, op.position)) >= MOVEMENT_PROGRESS_BLOCKS ** 2
            # Displacement counts only while a path is being followed, so
            # being pushed, falling or jittering in place is not progress.
            if moved and (payload.get("pathing") or navigation.get("pathing")):
                signal = signal or "movement"
            if moved or op.position is None:
                op.position = current
        inventory = payload.get("inventory")
        if not payload.get("lite") and isinstance(inventory, Mapping) and payload.get("inventory_complete") is not False:
            counts = {str(key): int(value) for key, value in inventory.items() if isinstance(value, int)}
            if op.inventory is None:
                op.start_inventory = dict(counts)
            elif counts != op.inventory:
                signal = signal or "inventory"
            op.inventory = counts
        return signal

    @staticmethod
    def _merge_facts(op: _Operation, payload: Mapping[str, Any]) -> None:
        facts = op.facts
        facts["source"] = "client"
        if op.position is not None:
            facts["position"] = [round(value, 1) for value in op.position]
        navigation = payload.get("navigation") if isinstance(payload.get("navigation"), Mapping) else None
        if navigation is not None:
            facts["navigation"] = {key: navigation[key] for key in _NAVIGATION_FACTS if key in navigation}
        for key in ("pathing", "builder_paused", "selected_item"):
            if key in payload:
                facts[key] = payload[key]
        active = [name for name, field in PROCESS_FIELDS.items() if payload.get(field) is True]
        if any(field in payload for field in PROCESS_FIELDS.values()):
            facts["processes"] = active
        construction = payload.get("construction_progress")
        if isinstance(construction, Mapping):
            facts["construction"] = {key: construction[key]
                                     for key in ("expected_cells", "matching_cells", "material_item")
                                     if key in construction}
        if op.inventory is not None and op.start_inventory is not None:
            delta = {item: count - op.start_inventory.get(item, 0)
                     for item, count in op.inventory.items() if count != op.start_inventory.get(item, 0)}
            delta.update({item: -count for item, count in op.start_inventory.items() if item not in op.inventory})
            ordered = sorted(delta.items(), key=lambda pair: -abs(pair[1]))
            facts["inventory_delta"] = dict(ordered[:FACT_DELTA_LIMIT])
            if len(ordered) > FACT_DELTA_LIMIT:
                facts["inventory_delta_omitted"] = len(ordered) - FACT_DELTA_LIMIT
        goal = payload.get("baritone_goal")
        if isinstance(goal, str):
            facts["goal"] = goal[:200]
        failing = op.path_failed - op.path_failed_at_progress
        if failing > 0:
            facts["failed_path_calculations"] = failing
        else:
            facts.pop("failed_path_calculations", None)
        op.phase = ExecutionSnapshots._phase(payload, navigation or {}, active, op.phase)
        if failing >= 2 and op.phase in {None, "calculating_path"}:
            op.phase = "path_calculation_failing"

    @staticmethod
    def _phase(payload: Mapping[str, Any], navigation: Mapping[str, Any], active: list[str],
               previous: str | None) -> str | None:
        if payload.get("lite") and not navigation and not active:
            return previous  # a lite frame carries no process evidence
        if payload.get("builder_paused") is True:
            return "builder_paused"
        if navigation.get("calculating") is True:
            return "calculating_path"
        if navigation.get("pending_break"):
            return "breaking"
        if navigation.get("pending_place"):
            return "placing"
        for name in ("builder", "mine", "get", "explore", "follow", "farm", "elytra"):
            if name in active:
                return _PROCESS_PHASES[name]
        if payload.get("pathing") is True or "goal" in active:
            return "moving"
        return None

    @staticmethod
    def _payload(op: _Operation) -> dict[str, Any]:
        now = time.monotonic()
        value: dict[str, Any] = {
            "operation_id": op.operation_id,
            "tool": op.tool,
            "action_id": op.action_id,
            "state": op.state,
            "phase": op.phase,
            "reason": op.reason,
            "observed_at": op.observed_at,
            "last_progress_at": op.last_progress_at,
            "control": op.control,
            "elapsed_s": round(now - op.started, 1),
            "facts": json.loads(json.dumps(op.facts, default=str)),
        }
        if op.last_progress_at is not None:
            value["since_progress_s"] = round(now - op.last_progress, 1)
            value["progress_signal"] = op.progress_signal
        return value


__all__ = ["ExecutionSnapshots", "TERMINAL_STATES"]
