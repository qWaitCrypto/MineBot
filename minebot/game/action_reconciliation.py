"""Action-level reconciliation for ambiguous Body dispatches.

The transport cannot know whether a server-side mutation ran before a socket
failed.  This module keeps the decision conservative: a known terminal event
or an authoritative block fact may settle a small set of block actions; every
other unresolved state stays typed ``unknown`` and is never replayed blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from minebot.contract import Action, Event, Position


class ActionReconciliationStatus(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionReconciliation:
    status: ActionReconciliationStatus
    evidence: dict[str, object]


# A dispatch for any of these actions can change world, inventory, or entity
# state.  They must not use the transport's generic reconnect-and-replay path.
MUTATING_ACTIONS = frozenset(
    {
        "navigateTo",
        "navigationMutationDecision",
        "useItem",
        "rangedAttack",
        "attackEntity",
        "engageEntity",
        "dropItem",
        "handoffItem",
        "moveItem",
        "craftItem",
        "furnaceTransfer",
        "containerTransfer",
        "mineBlock",
        "placeBlock",
        "igniteBlock",
        "sowCrop",
    }
)


TERMINAL_EVENT_BY_ACTION = {
    "moveTo": "moveDone",
    "navigateTo": "navigateDone",
    "followEntity": "followDone",
    "engageEntity": "engageDone",
    "lookAt": "lookDone",
    "jump": "jumpDone",
    "selectSlot": "selectSlotDone",
    "selectItem": "selectItemDone",
    "stop": "stopDone",
    "useItem": "useDone",
    "rangedAttack": "rangedDone",
    "attackEntity": "attackDone",
    "dropItem": "dropDone",
    "handoffItem": "handoffDone",
    "moveItem": "moveItemDone",
    "craftItem": "craftDone",
    "furnaceTransfer": "furnaceDone",
    "containerTransfer": "containerDone",
    "mineBlock": "mineDone",
    "placeBlock": "placeDone",
    "igniteBlock": "igniteDone",
    "sowCrop": "sowDone",
}


_CLEAR_BLOCKS = frozenset({"air", "cave_air", "void_air"})
_BLOCK_TERMINAL_ACTIONS = frozenset({"mineBlock", "placeBlock", "igniteBlock", "sowCrop"})


def is_mutating_action(action_name: str) -> bool:
    return action_name in MUTATING_ACTIONS


def terminal_event_name(action_name: str) -> str | None:
    return TERMINAL_EVENT_BY_ACTION.get(action_name)


def block_probe_position(action: Action) -> Position | None:
    """Return the block cell whose terminal state settles a block action."""

    if action.name not in _BLOCK_TERMINAL_ACTIONS:
        # Composite navigation may mutate several cells and has no single
        # target block whose current type can settle the action.  Probing its
        # goal here creates false evidence and an unnecessary transport read.
        return None
    target = _position(action.params.get("target"))
    if target is None:
        return None
    if action.name == "sowCrop":
        return (target[0], target[1] + 1, target[2])
    return target


def classify_authoritative_block(
    action: Action,
    current_type: str,
) -> ActionReconciliation:
    """Classify block-only actions from one authoritative block read.

    The caller must provide a complete ``blockAt`` fact.  A changed block that
    is not the declared success state is deliberately ambiguous; it may be a
    concurrent world change and must not authorize a replay.
    """

    current = str(current_type or "unknown").removeprefix("minecraft:")
    params = action.params
    target = block_probe_position(action)
    expected = str(params.get("block_type") or "").removeprefix("minecraft:")
    evidence: dict[str, object] = {
        "scope": "blockAt",
        "target": list(target) if target is not None else None,
        "current_type": current,
        "expected_type": expected or None,
    }

    if action.name == "mineBlock":
        if not target or not expected or expected == "unknown":
            return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)
        if current == expected:
            return ActionReconciliation(ActionReconciliationStatus.NOT_APPLIED, evidence)
        if current in _CLEAR_BLOCKS:
            evidence["success_fact"] = "target_clear"
            return ActionReconciliation(ActionReconciliationStatus.APPLIED, evidence)
        return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)

    if action.name == "placeBlock":
        if not target or not expected or expected == "unknown":
            return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)
        if current == expected:
            evidence["success_fact"] = "target_matches_placed_type"
            return ActionReconciliation(ActionReconciliationStatus.APPLIED, evidence)
        if current in _CLEAR_BLOCKS:
            return ActionReconciliation(ActionReconciliationStatus.NOT_APPLIED, evidence)
        return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)

    if action.name == "igniteBlock":
        if not target:
            return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)
        if current == "fire":
            evidence["success_fact"] = "target_fire"
            return ActionReconciliation(ActionReconciliationStatus.APPLIED, evidence)
        return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)

    if action.name == "sowCrop":
        crop_block = str(params.get("crop_block") or "").removeprefix("minecraft:")
        if not target or not crop_block:
            return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)
        evidence["crop_target"] = list(target)
        if current == crop_block:
            evidence["success_fact"] = "crop_matches_expected_type"
            return ActionReconciliation(ActionReconciliationStatus.APPLIED, evidence)
        if current in _CLEAR_BLOCKS:
            return ActionReconciliation(ActionReconciliationStatus.NOT_APPLIED, evidence)
        return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)

    return ActionReconciliation(ActionReconciliationStatus.UNKNOWN, evidence)


def terminal_event_is_applied(event: Event) -> bool:
    """Return true only for an explicit successful terminal observation."""

    data = event.data
    if data.get("success") is True:
        return True
    if data.get("arrived") is True:
        return True
    if data.get("completed") is True:
        return True
    return str(data.get("stopped_reason") or data.get("reason") or "") in {
        "arrived",
        "completed",
        "killed",
        "reconciled",
    }


def reconciled_terminal_event(
    action: Action,
    reconciliation: ActionReconciliation,
) -> Event | None:
    """Build a terminal event only when an authoritative world fact settled it."""

    if reconciliation.status is not ActionReconciliationStatus.APPLIED:
        return None
    name = terminal_event_name(action.name)
    if name is None:
        return None
    evidence = dict(reconciliation.evidence)
    data: dict[str, Any] = {
        "action_id": action.id,
        "success": True,
        "stopped_reason": "reconciled",
        "reconciliation": "authoritative_world",
        **evidence,
    }
    if action.name == "mineBlock":
        data.update(
            {
                "target": action.params.get("target"),
                "block_type": action.params.get("block_type"),
                "block_now": evidence.get("current_type"),
                "block_gone": True,
            }
        )
    elif action.name == "placeBlock":
        data.update(
            {
                "target": action.params.get("target"),
                "expected_type": action.params.get("block_type"),
                "block_at_target": evidence.get("current_type"),
            }
        )
    elif action.name == "igniteBlock":
        data.update({"target": action.params.get("target"), "block_at_target": "fire"})
    elif action.name == "sowCrop":
        data.update(
            {
                "target": action.params.get("target"),
                "crop_pos": evidence.get("crop_target"),
                "expected_type": action.params.get("crop_block"),
                "block_at_crop": evidence.get("current_type"),
            }
        )
    return Event(seq=0, tick=0, bot="", name=name, data=data)


def _position(value: object) -> Position | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        return (int(value[0]), int(value[1]), int(value[2]))
    except (TypeError, ValueError):
        return None


__all__ = [
    "ActionReconciliation",
    "ActionReconciliationStatus",
    "MUTATING_ACTIONS",
    "TERMINAL_EVENT_BY_ACTION",
    "block_probe_position",
    "classify_authoritative_block",
    "is_mutating_action",
    "reconciled_terminal_event",
    "terminal_event_is_applied",
    "terminal_event_name",
]
