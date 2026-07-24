"""Canonical Body/Agent message schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class Action:
    id: str
    name: str
    params: JsonObject = field(default_factory=dict)

    @classmethod
    def create(cls, name: str, params: JsonObject | None = None) -> "Action":
        return cls(id=str(uuid4()), name=name, params=params or {})

    def to_payload(self) -> JsonObject:
        return {"type": "action", "id": self.id, "name": self.name, "params": self.params}


@dataclass(frozen=True)
class Result:
    id: str | None
    bot: str
    type: Literal["result"]
    ok: bool
    accepted: bool
    complete: bool
    data: JsonObject = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class Event:
    seq: int
    tick: int
    bot: str
    name: str
    data: JsonObject = field(default_factory=dict)
    type: Literal["event"] = "event"


@dataclass(frozen=True)
class BodyState:
    bot: str
    pos: tuple[float, float, float]
    yaw: float | None
    pitch: float | None
    health: float
    food: int
    oxygen: int | None
    inventory_raw: str
    inventory_hash: str
    effects: list[JsonObject] | None
    time: int
    weather: str | None
    dimension: str | None
    complete: bool
    sleeping: bool | None = None
    missing: bool = False
    selected_slot: int | None = None
    # Compact authoritative facts used by the autonomy-quality trace.  These
    # fields are optional for compatibility with older Body providers; the
    # production Scarpet provider emits them from the same server-side state
    # read as the position and inventory hash.
    inventory_counts: dict[str, int] | None = None
    selected_item: str | None = None
    offhand_item: str | None = None
    body_owner: str | None = None
    pending_action_count: int | None = None
    # Server-owned survival state.  A non-null value means the last survival
    # reflex could not resolve its hazard; ordinary physical actions must stay
    # blocked until a recovery transaction clears it.
    hazard_unresolved: JsonObject | None = None

    @classmethod
    def from_envelope_data(cls, bot: str, complete: bool, data: JsonObject) -> "BodyState":
        inventory_raw = str(data.get("inventory_raw") or "")
        inventory_hash = str(data.get("inventory_hash") or stable_hash(inventory_raw))
        pos = data["pos"]
        if len(pos) != 3:
            raise ValueError(f"BodyState.pos must have 3 values, got {pos!r}")
        return cls(
            bot=bot,
            pos=(float(pos[0]), float(pos[1]), float(pos[2])),
            yaw=_maybe_float(data.get("yaw")),
            pitch=_maybe_float(data.get("pitch")),
            health=float(data["health"]),
            food=int(data["food"]),
            oxygen=_maybe_int(data.get("oxygen")),
            inventory_raw=inventory_raw,
            inventory_hash=inventory_hash,
            effects=data.get("effects"),
            time=int(data["time"]),
            weather=data.get("weather"),
            dimension=data.get("dimension"),
            complete=complete,
            sleeping=None if "sleeping" not in data or data.get("sleeping") is None else bool(data.get("sleeping")),
            missing=bool(data.get("missing", False)),
            selected_slot=_maybe_int(data.get("selected_slot")),
            inventory_counts=_inventory_counts(data.get("inventory_counts")),
            selected_item=_maybe_string(data.get("selected_item")),
            offhand_item=_maybe_string(data.get("offhand_item")),
            body_owner=_maybe_string(data.get("body_owner")),
            pending_action_count=_maybe_int(data.get("pending_action_count")),
            hazard_unresolved=(
                dict(data["hazard_unresolved"])
                if isinstance(data.get("hazard_unresolved"), dict)
                else None
            ),
        )


@dataclass(frozen=True)
class PerceptionResult:
    bot: str
    scope: str
    type: Literal["perception"]
    ok: bool
    complete: bool
    data: JsonObject = field(default_factory=dict)
    uncertainty: list[JsonObject] | None = None
    next: str | None = None
    error: str | None = None


def perception_next_cursor(perception: PerceptionResult, *data_keys: str) -> object | None:
    """Return the next-page cursor from a perception envelope.

    Static RCON pages mirror their resume cursor through the protocol envelope
    `next`; older and scope-specific payloads may also carry it inside `data`.
    Keeping this lookup in the shared contract prevents callers from silently
    stopping after page one when only one surface is populated.
    """

    keys = data_keys or ("nextStart", "next")
    for key in keys:
        value = perception.data.get(key)
        if value is not None:
            return value
    return perception.next


@dataclass(frozen=True)
class InventorySlot:
    slot: int
    item: str | None
    count: int
    empty: bool
    slot_type: str | None = None
    slot_label: str | None = None
    stack_raw: str | None = None

    @classmethod
    def from_payload(cls, data: JsonObject) -> "InventorySlot":
        return cls(
            slot=int(data["slot"]),
            item=data.get("item"),
            count=int(data.get("count") or 0),
            empty=bool(data.get("empty")),
            slot_type=data.get("slotType"),
            slot_label=data.get("slotLabel"),
            stack_raw=data.get("stackRaw"),
        )


@dataclass(frozen=True)
class ToolResult:
    """Unified result envelope for Body transactions exposed as tools."""

    success: bool
    reason: str
    can_retry: bool
    next_suggestion: str | None = None
    metrics: JsonObject | None = None

    def to_payload(self) -> JsonObject:
        return {
            "success": self.success,
            "reason": self.reason,
            "canRetry": self.can_retry,
            "nextSuggestion": self.next_suggestion,
            "metrics": self.metrics,
        }


# ---------------------------------------------------------------------------
# Candidate-skip vocabulary (agent-loop.md §6: neutral sentinel).
#
# A "candidate skip" is a result that means "THIS specific target/candidate is
# unsuitable — pick another", NOT "the task is failing". For progress accounting
# it is NEUTRAL: the weld must not feed it to the failure-storm sensor, and a
# composition orchestrator should skip the candidate and keep going. These are
# deliberately NARROW — genuine failures (incomplete perception, transport error,
# owner_busy, invalid input, "nothing found at all") are intentionally absent and
# stay counted. `search_block_not_found` is excluded on purpose: it means there
# are no candidates, which the orchestrator returns (not skips).
CANDIDATE_SKIP_REASONS: frozenset[str] = frozenset(
    {
        "mine_approach_out_of_range",
        "collect_no_inventory_delta",
        "stand_point_unreachable",
        "search_block_no_stand_point",
        "search_block_out_of_range",
        "search_block_target_lost",
        # The target can become occluded after navigation (for example when
        # a neighboring block remains in the aim ray).  This invalidates only
        # the selected candidate; resource orchestration must try another
        # authoritative stand/target before declaring the domain exhausted.
        "target_occluded",
    }
)

CANDIDATE_SKIP_PREFIXES: tuple[str, ...] = (
    "break_denied:",
    "navigation_blocked:",
    "navigation_replan_required:",
    "search_block_navigation_failed:",
    "mine_approach_failed:",
)


def is_candidate_skip(reason: str | None) -> bool:
    """True if ``reason`` marks an unsuitable candidate (neutral), not a failure."""
    if not reason:
        return False
    if reason in CANDIDATE_SKIP_REASONS:
        return True
    return reason.startswith(CANDIDATE_SKIP_PREFIXES)


def stable_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _maybe_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _maybe_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _maybe_string(value: Any) -> str | None:
    return None if value is None else str(value).removeprefix("minecraft:")


def _inventory_counts(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in value.items():
        try:
            clean_count = int(count)
        except (TypeError, ValueError):
            continue
        if clean_count <= 0:
            continue
        counts[str(key).removeprefix("minecraft:")] = clean_count
    return counts
