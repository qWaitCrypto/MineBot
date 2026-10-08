"""Keep small tree work continuous; leave bulk mining to upstream MineProcess."""

from __future__ import annotations

import itertools
import math
import time
from typing import TYPE_CHECKING

from minebot.contract import action_with_timeout, action_wait_timeout_s
from minebot.body.world_read import read_block_facts
from minebot.contract import Action, Body, Position, ToolResult, execution_checkpoint, terminal_event_to_tool_result

if TYPE_CHECKING:
    from minebot.body.resource_collection import ResourceCollectionConfig


def resource_cluster(
    body: Body, anchor: Position, block_types: tuple[str, ...], limit: int, *, tree: bool = False,
) -> list[Position]:
    """Read a bounded connected prefix, including logs above the search page.

    A full page is a reason to use demand-based mining, never a failed task.
    """
    offsets = tuple(itertools.product((-1, 0, 1), repeat=3)) if tree else (
        (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
    wanted = set(block_types)
    found, seen, frontier = [], set(), {anchor}
    while frontier:
        execution_checkpoint()
        cells = sorted(frontier)
        frontier = set()
        facts = read_block_facts(body, cells, failure_label="resource_cluster")
        for pos in cells:
            seen.add(pos)
            block = str(facts[pos].data.get("type", "")).removeprefix("minecraft:")
            if block not in wanted:
                continue
            if len(found) >= limit:
                return found
            found.append(pos)
            for offset in offsets:
                neighbor = tuple(pos[i] + offset[i] for i in range(3))
                if neighbor not in seen:
                    frontier.add(neighbor)
        frontier.difference_update(seen)
    return found


def collect_native_cluster(
    body: Body, *, block_types: tuple[str, ...], expected_drops: tuple[str, ...],
    remaining_count: int, dry: bool, config: ResourceCollectionConfig,
) -> ToolResult | None:
    """Return None to retain the governed legacy path outside disposable worlds."""
    if dry:
        return None
    process = body.perceive("baritoneState", {})
    if not process.ok or process.data.get("disposable_world") is not True:
        return None
    blocks = tuple(str(b).removeprefix("minecraft:") for b in block_types)
    drops = tuple(str(b).removeprefix("minecraft:") for b in expected_drops)
    tree = all(b.endswith(("_log", "_stem")) for b in blocks)
    def inventory_total() -> int:
        inventory = body.get_state().inventory_counts or {}
        return sum(inventory.get(item, 0) for item in drops)

    before = inventory_total()
    deadline = time.monotonic() + config.max_wall_s
    attempts, excluded, actions_used = [], set(), 0

    def result(success: bool, reason: str) -> ToolResult:
        current = inventory_total()
        return ToolResult(success, reason, can_retry=not success, metrics={
            "collected": current - before, "requested_count": remaining_count,
            "remaining_count": max(0, remaining_count - (current - before)),
            "attempts": attempts, "collection_strategy": "native_resource_collection"})

    while time.monotonic() < deadline and actions_used < config.mutation_budget:
        execution_checkpoint()
        current = inventory_total()
        if current - before >= remaining_count:
            return result(True, "resource_collected")
        needed = remaining_count - (current - before)
        timeout = min(300.0, deadline - time.monotonic())
        if timeout <= 0:
            break
        cluster: list[Position] = []
        anchor = None
        if tree:
            scan = body.perceive("findBlocks", {"block_types": list(blocks),
                "radius": config.search_radius, "y_radius": min(64, max(16, config.search_radius)), "limit": 256})
            if not scan.ok:
                return result(False, scan.error or "resource_search_failed")
            candidates = [tuple(int(cell[k]) for k in ("x", "y", "z"))
                          for cell in scan.data.get("blocks", [])]
            candidates = [pos for pos in candidates if pos not in excluded]
            if not candidates:
                return result(False, "resource_candidates_not_found")
            origin = body.get_state().pos
            anchor = min(candidates, key=lambda pos: math.dist(origin, pos))
            # Spend a little of the remaining task time finishing the current
            # tree, rather than making every connected forest an obligation.
            # One extra log per second is a conservative work estimate, not a
            # tree-size classification. Probe one beyond the allowance.
            finish_allowance = int(min(10.0, timeout * 0.1))
            probe_limit = min(257, needed + finish_allowance + 1)
            cluster = resource_cluster(body, anchor, blocks, probe_limit, tree=True)
            if not cluster:
                excluded.add(anchor)
                continue
            if len(cluster) >= probe_limit or len(cluster) > 256:
                cluster = []
        timeout = min(300.0, deadline - time.monotonic())
        if timeout <= 0:
            break
        if cluster:
            action = Action.create("collectBlocks", {"targets": [list(pos) for pos in cluster],
                "expected_block_ids": list(blocks), "expected_item_ids": list(drops), "timeout_s": timeout})
        else:
            # MineProcess owns movement, local resource choice and pickup.
            # Its inventory goal is finite even for a mountain or giant tree.
            native_blocks, item = list(blocks), drops[0]
            if tree and anchor is not None:
                fact = read_block_facts(body, [anchor], failure_label="resource_cluster")[anchor]
                item = str(fact.data["type"]).removeprefix("minecraft:")
                native_blocks = [item]
            action = Action.create("mineResource", {"blocks": native_blocks, "item": item,
                "quantity": needed, "timeout_s": timeout})
        action = action_with_timeout(action, timeout)
        accepted = body.execute(action)
        if not accepted.ok or not accepted.accepted:
            return result(False, accepted.error or "body_rejected")
        terminal = terminal_event_to_tool_result(body.await_action_terminal(action.id, timeout_s=action_wait_timeout_s(action)))
        attempts.append({"target": list(anchor) if anchor is not None else None,
                         "targets": [list(pos) for pos in cluster], "action": action.name,
                         "mine": terminal.to_payload()})
        actions_used += 1
        if not terminal.success:
            return result(False, terminal.reason)
    complete = inventory_total() - before >= remaining_count
    return result(complete, "resource_collected" if complete else "resource_domain_budget_exhausted")
