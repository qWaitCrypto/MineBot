"""Observer-only Body telemetry for the live panel.

Writes ``body_progress`` and ``world_edit`` records to the trace sink.  It never
changes Body state caches, Agent context or action settlement; failures are
swallowed because an observer cannot own execution.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from minebot.contract.execution import tool_observation_scope
from minebot.panel.projection import progress_digest, project_progress

HEARTBEAT_S = 2.0


def panel_telemetry_enabled(env: Mapping[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get("MINEBOT_PANEL_TELEMETRY", "1").strip() != "0"


class PanelTelemetry:
    def __init__(self, trace: Any, *, enabled: bool | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.trace = trace
        self.enabled = panel_telemetry_enabled() if enabled is None else enabled
        self._clock = clock
        self._lock = threading.Lock()
        self._last_digest: str | None = None
        self._last_emit = 0.0
        self._last_inventory: dict[str, Any] | None = None
        self._edits_seen: set[tuple[str, int]] = set()

    def scoped(self, callback: Callable[..., Any], call_id: str | None) -> Callable[..., Any]:
        """Run ``callback`` with native progress for ``call_id`` routed to the trace."""
        if not self.enabled or not call_id:
            return callback

        def observed(*args: Any, **kwargs: Any) -> Any:
            with tool_observation_scope(call_id, lambda payload: self.observe(call_id, payload)):
                return callback(*args, **kwargs)

        return observed

    def observe(self, call_id: str | None, payload: Mapping[str, Any]) -> None:
        if not self.enabled or payload.get("type") not in ("progress", None):
            return
        try:
            self._observe(call_id, str(payload.get("action_id") or ""), payload, source="action")
        except Exception:
            pass

    def sample_idle(self, sample: Mapping[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self._observe(None, "", sample, source="idle")
        except Exception:
            pass

    def _emit(self, event: str, **fields: Any) -> None:
        emit = getattr(self.trace, "emit_transient", None) or self.trace.emit
        emit(event, **fields)

    def _observe(self, call_id: str | None, action_id: str, payload: Mapping[str, Any], *, source: str) -> None:
        for edit in payload.get("world_edits") or ():
            if not isinstance(edit, Mapping):
                continue
            key = (action_id, int(edit.get("index", -1)))
            with self._lock:
                if key in self._edits_seen:
                    continue
                self._edits_seen.add(key)
            self._emit("world_edit", tool_call_id=call_id, action_id=action_id or None, **edit)
        frame = project_progress(payload)
        inventory = frame.pop("inventory_counts", None)
        now = self._clock()
        with self._lock:
            inventory_changed = inventory is not None and inventory != self._last_inventory
            digest = progress_digest({**frame, "_call": call_id, "_source": source})
            if not inventory_changed and digest == self._last_digest and now - self._last_emit < HEARTBEAT_S:
                return
            if inventory_changed:
                self._last_inventory = dict(inventory)
                frame["inventory_counts"] = inventory
            self._last_digest = digest
            self._last_emit = now
        self._emit(
            "body_progress",
            tool_call_id=call_id,
            action_id=action_id or None,
            source=source,
            lite=bool(payload.get("lite")),
            **frame,
        )


__all__ = ["PanelTelemetry", "panel_telemetry_enabled"]
