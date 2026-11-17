"""Compact observer projection of native Body samples.

Shared by the runtime (which writes ``body_progress`` trace records) and the
live panel (which reads them).  Pure functions only: no IO, no clocks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

PROCESS_FIELDS = {
    "goal": "goal_active",
    "builder": "builder_active",
    "mine": "mine_active",
    "get": "get_active",
    "explore": "explore_active",
    "follow": "follow_active",
    "farm": "farm_active",
    "elytra": "elytra_active",
}
NAVIGATION_FIELDS = (
    "src", "dest", "movement", "path_position", "path_movements", "pending_break", "pending_place",
    "idle_ms", "repeated_failures", "calculating", "feet_block", "last_failure", "path_calculations",
)
SCALAR_FIELDS = (
    "on_ground", "health", "food", "oxygen", "selected_item", "selected_slot", "offhand_item", "fall_flying", "dimension",
    "time", "weather", "pathing", "baritone_goal", "builder_paused", "any_process_active",
    "allow_break", "allow_place", "allow_inventory", "sampled_at_ms", "last_path_event",
)
# Native frame keys that exist for observers only and must never reach Body state caches.
OBSERVER_ONLY_FIELDS = frozenset({"lite", "world_edits", "path_events", "last_path_event"})
NEAR_ENTITY_LIMIT = 8


def project_progress(sample: Mapping[str, Any]) -> dict[str, Any]:
    """Project one native sample to the fields the panel draws."""
    frame: dict[str, Any] = {}
    position = sample.get("position")
    if isinstance(position, (list, tuple)) and len(position) == 3:
        frame["pos"] = [round(float(value), 2) for value in position]
    for key in ("yaw", "pitch"):
        if isinstance(sample.get(key), (int, float)):
            frame[key] = round(float(sample[key]), 1)
    for key in SCALAR_FIELDS:
        if key in sample:
            frame[key] = sample[key]
    processes = {name: bool(sample[field]) for name, field in PROCESS_FIELDS.items() if field in sample}
    if processes:
        frame["processes"] = processes
    navigation = sample.get("navigation")
    if isinstance(navigation, Mapping):
        frame["navigation"] = {key: navigation[key] for key in NAVIGATION_FIELDS if key in navigation}
    if isinstance(sample.get("path_events"), Mapping):
        frame["path_events"] = dict(sample["path_events"])
    construction = sample.get("construction_progress")
    if isinstance(construction, Mapping):
        frame["construction_progress"] = {
            key: construction[key]
            for key in ("expected_cells", "observed_cells", "matching_cells", "complete", "material_item")
            if key in construction
        }
    if isinstance(sample.get("effects"), list):
        frame["effects"] = [
            {"id": str(effect.get("id", "")).removeprefix("minecraft:"), "amplifier": effect.get("amplifier")}
            for effect in sample["effects"] if isinstance(effect, Mapping)
        ]
    entities = sample.get("entities")
    if isinstance(entities, list):
        frame["entities_near"] = near_entities(entities)
    slots = sample.get("inventory_slots")
    if isinstance(slots, list):
        hotbar: list[Any] = [None] * 9
        for slot in slots:
            if isinstance(slot, Mapping) and slot.get("slotType") == "hotbar" and not slot.get("empty"):
                index = int(slot.get("slot", -1))
                if 0 <= index < 9:
                    hotbar[index] = [str(slot.get("item", "")).removeprefix("minecraft:"), slot.get("count", 0)]
        frame["hotbar"] = hotbar
    inventory = sample.get("inventory")
    if isinstance(inventory, Mapping):
        frame["inventory_counts"] = {str(key): value for key, value in inventory.items()}
    return frame


def near_entities(entities: list[Any], limit: int = NEAR_ENTITY_LIMIT) -> list[dict[str, Any]]:
    observed = [entity for entity in entities if isinstance(entity, Mapping)]
    observed.sort(key=lambda entity: float(entity.get("dist2") or 0.0))
    near = []
    for entity in observed[:limit]:
        item: dict[str, Any] = {
            "type": entity.get("type"),
            "dist": round(float(entity.get("dist2") or 0.0) ** 0.5, 1),
            "hostile": bool(entity.get("hostile")),
        }
        if entity.get("name") and entity.get("name") != entity.get("type"):
            item["name"] = entity.get("name")
        pos = entity.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 3:
            item["pos"] = [round(float(value), 1) for value in pos]
        if isinstance(entity.get("health"), (int, float)):
            item["health"] = entity["health"]
        near.append(item)
    return near


def progress_digest(frame: Mapping[str, Any]) -> str:
    """Identity of a frame for de-duplication; the sample clock is not a change."""
    return json.dumps({key: value for key, value in frame.items() if key != "sampled_at_ms"},
                      sort_keys=True, default=str)


def state_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A native frame without its observer-only keys."""
    return {key: value for key, value in payload.items() if key not in OBSERVER_ONLY_FIELDS}


__all__ = [
    "OBSERVER_ONLY_FIELDS",
    "near_entities",
    "progress_digest",
    "project_progress",
    "state_fields",
]
