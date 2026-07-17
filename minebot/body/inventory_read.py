"""Shared authoritative inventory reads for Body transactions."""

from __future__ import annotations

from minebot.contract import Body, InventorySlot, PerceptionResult, ToolResult, perception_next_cursor


def read_inventory_slots(body: Body, page_size: int = 12) -> PerceptionResult:
    start: int | None = 0
    slots: list[dict[str, object]] = []
    last: PerceptionResult | None = None
    while start is not None:
        last = body.perceive("inventory", {"start": start, "limit": page_size})
        if not last.ok:
            return last
        slots.extend(dict(item) for item in last.data.get("slots") or [])
        next_start = perception_next_cursor(last)
        start = int(next_start) if next_start is not None else None
    if last is None:
        return PerceptionResult(
            bot=body.bot_name,
            scope="inventory",
            type="perception",
            ok=False,
            complete=True,
            error="no pages read",
        )
    data = dict(last.data)
    data["slots"] = slots
    return PerceptionResult(
        bot=last.bot,
        scope=last.scope,
        type=last.type,
        ok=last.ok,
        complete=last.complete,
        data=data,
        uncertainty=last.uncertainty,
        next=last.next,
        error=last.error,
    )


def inventory_counts(slots: list[InventorySlot]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for slot in slots:
        if slot.empty or not slot.item:
            continue
        item = str(slot.item).removeprefix("minecraft:")
        counts[item] = counts.get(item, 0) + slot.count
    return counts


def read_inventory_counts(body: Body, page_size: int = 12) -> dict[str, int] | ToolResult:
    inventory = read_inventory_slots(body, page_size=page_size)
    if not (inventory.ok and inventory.complete):
        return ToolResult(
            success=False,
            reason="perception_failed",
            can_retry=True,
            next_suggestion="retry the authoritative inventory read before deciding whether pickup completed",
            metrics={
                "scope": inventory.scope,
                "ok": inventory.ok,
                "complete": inventory.complete,
                "error": inventory.error,
                "uncertainty": inventory.uncertainty,
            },
        )
    return inventory_counts(
        [InventorySlot.from_payload(slot) for slot in inventory.data.get("slots") or []]
    )


__all__ = ["inventory_counts", "read_inventory_counts", "read_inventory_slots"]
