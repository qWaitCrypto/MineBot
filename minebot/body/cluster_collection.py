"""Finish bounded local resource clusters; leave bulk mining to MineProcess."""

from __future__ import annotations

import itertools
import math
import re
import time
from typing import TYPE_CHECKING

from minebot.contract import action_with_timeout, action_wait_timeout_s
from minebot.body.native_admission import harvest_tool_denial
from minebot.body.world_read import read_block_facts
from minebot.game.errors import BodyActionTimeoutError
from minebot.contract import Action, Body, Position, ToolResult, execution_checkpoint, terminal_event_to_tool_result

if TYPE_CHECKING:
    from minebot.body.resource_collection import ResourceCollectionConfig

# A native mining process that gains nothing for this long, without having
# travelled, is reported instead of being left to run out its deadline.
NO_PROGRESS_WINDOW_S = 90.0
NO_PROGRESS_TRAVEL_BLOCKS = 24.0
# Baritone keeps recalculating a path it cannot find. This many failures in a
# row with nothing gained is its own evidence that the target is out of reach.
PATH_FAILURE_LIMIT = 5
# Unfinished clusters in a row that added nothing before the collection gives up.
FRUITLESS_CLUSTER_LIMIT = 2
TOOL_LOST = "tool_lost"
_PROGRESS_POLL_S = 2.0


def resource_cluster(
    body: Body, anchor: Position, block_types: tuple[str, ...], limit: int, *, tree: bool = False,
    include_neighbors: bool = False,
) -> list[Position]:
    """Read a bounded connected prefix, including logs above the search page.

    A full page is a reason to use demand-based mining, never a failed task.
    """
    offsets = tuple(itertools.product((-1, 0, 1), repeat=3)) if tree else (
        (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
    wanted = set(block_types)
    found, seen, frontier = [], set(), {anchor}
    if include_neighbors:
        # MineProcess may already have removed the last selected resource cell.
        frontier.update(tuple(anchor[i] + offset[i] for i in range(3)) for offset in offsets)
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


def _path_state(body: Body) -> tuple[int, str, str, int | None]:
    """Baritone's failed path calculations, goal, failure name and carried scaffold blocks."""
    try:
        read = body.perceive("baritoneState", {})
    except Exception:
        return 0, "", "", None
    data = read.data if read.ok and isinstance(read.data, dict) else {}
    events = data.get("path_events")
    failed = events.get("CALC_FAILED") if isinstance(events, dict) else 0
    navigation = data.get("navigation") if isinstance(data.get("navigation"), dict) else {}
    scaffold = data.get("scaffold_blocks")
    return ((failed if isinstance(failed, int) else 0), str(data.get("baritone_goal") or "")[:200],
            str(navigation.get("last_failure") or ""), scaffold if type(scaffold) is int else None)


_GOAL_POSITION = re.compile(r"x=(-?\d+),\s*y=(-?\d+),\s*z=(-?\d+)")


def _unreachable_facts(goal: str, feet: Position | tuple[float, ...], native_failure: str) -> dict:
    """Where the target is relative to the Body. Measurements, not a diagnosis."""
    facts: dict = {"goal": goal}
    if native_failure:
        facts["native_failure"] = native_failure
    match = _GOAL_POSITION.search(goal)
    if match is not None:
        target = tuple(int(value) for value in match.groups())
        facts["target"] = list(target)
        facts["target_above_feet"] = target[1] - math.floor(feet[1])
        facts["target_horizontal_distance"] = round(
            math.dist((target[0] + 0.5, target[2] + 0.5), (feet[0], feet[2])), 1)
    return facts


def _await_with_progress(body: Body, action: Action, inventory_total,
                         tool_lost=lambda: False) -> tuple[ToolResult, dict | None]:
    """Wait for a native mining action, stopping it when it stops producing.

    Returns the terminal and, when the action was stopped for lack of
    progress, the observed facts. Progress is an inventory gain; travelling
    resets the window so a long walk to a distant resource is not a stall.
    ``tool_lost`` says the Body no longer owns a tool that can harvest the
    target: the action is stopped at once instead of after a stall window.
    """
    deadline = time.monotonic() + action_wait_timeout_s(action)
    best, mark_at, mark_pos = inventory_total(), time.monotonic(), body.get_state().pos
    failed_mark = _path_state(body)[0]
    native_failure = ""
    while True:
        execution_checkpoint()
        remaining = deadline - time.monotonic()
        try:
            event = body.await_action_terminal(action.id, timeout_s=max(0.01, min(_PROGRESS_POLL_S, remaining)))
            return terminal_event_to_tool_result(event), None
        except BodyActionTimeoutError:
            if remaining <= _PROGRESS_POLL_S:
                raise
        state = body.get_state()
        total = inventory_total()
        failed, goal, last_failure, scaffold = _path_state(body)
        native_failure = last_failure or native_failure
        if total > best or math.dist(state.pos, mark_pos) >= NO_PROGRESS_TRAVEL_BLOCKS:
            best, mark_at, mark_pos, failed_mark = max(best, total), time.monotonic(), state.pos, failed
            continue
        stalled_s = time.monotonic() - mark_at
        unreachable = failed - failed_mark >= PATH_FAILURE_LIMIT
        broke = tool_lost()
        if not broke and not unreachable and stalled_s < NO_PROGRESS_WINDOW_S:
            continue
        reason = TOOL_LOST if broke else "path_unreachable" if unreachable else "no_progress"
        body.interrupt(reason)
        event = body.await_action_terminal(action.id, timeout_s=30.0)
        facts = {
            "reason": reason,
            "stalled_s": round(stalled_s, 1), "inventory_total": total,
            "position": [round(float(value), 1) for value in state.pos],
            "selected_item": state.selected_item,
        }
        if unreachable:
            facts.update(failed_path_calculations=failed - failed_mark,
                         **_unreachable_facts(goal, state.pos, native_failure))
            if scaffold is not None:
                # Counted by the client from Baritone's own list of placeable blocks.
                facts["scaffold_blocks"] = scaffold
        return terminal_event_to_tool_result(event), facts


def collect_native_cluster(
    body: Body, *, block_types: tuple[str, ...], expected_drops: tuple[str, ...],
    remaining_count: int, dry: bool, config: ResourceCollectionConfig,
    unreachable: set[Position] | None = None,
) -> ToolResult | None:
    """Return None to retain the governed legacy path outside disposable worlds.

    ``unreachable`` is the task's memory of targets no path reached. They are
    skipped until the caller asks to retry them, so one such block cannot be
    selected again by every later call.
    """
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
    if unreachable is not None and config.retry_unreachable:
        unreachable.clear()
    avoided = {tuple(int(value) for value in pos) for pos in config.avoid} | set(unreachable or ())
    attempts, excluded, actions_used = [], set(avoided), 0
    unfinished: list[dict] = []
    fruitless = 0
    finish_target: Position | None = None

    def result(success: bool, reason: str, stalled: dict | None = None) -> ToolResult:
        current = inventory_total()
        metrics = {
            "collected": current - before, "requested_count": remaining_count,
            "remaining_count": max(0, remaining_count - (current - before)),
            "attempts": attempts, "collection_strategy": "native_resource_collection"}
        if unfinished:
            # Mined but not fully picked up, or abandoned part-way: drops may still lie there.
            metrics["unfinished_clusters"] = unfinished[-8:]
            metrics["unfinished_cluster_count"] = len(unfinished)
        if avoided:
            # Said every time, so a missing resource is not mistaken for an absent one.
            metrics["avoided_targets"] = [list(pos) for pos in sorted(avoided)[:16]]
            metrics["avoided_target_count"] = len(avoided)
        suggestion = None
        if stalled is not None:
            # Top level, so it survives the bounded projection the Agent reads.
            metrics["stalled"] = stalled
            if stalled.get("reason") == "path_unreachable":
                where = ""
                if "target_above_feet" in stalled:
                    where = (f" The target {stalled['target']} is {stalled['target_above_feet']} blocks above the "
                             f"Body's feet and {stalled['target_horizontal_distance']} blocks away horizontally; "
                             f"the Body holds {stalled.get('selected_item') or 'nothing'}.")
                    if "scaffold_blocks" in stalled:
                        where += (f" It carries {stalled['scaffold_blocks']} blocks that Baritone may place "
                                  "to climb or bridge.")
                suggestion = (
                    f"Baritone failed {stalled.get('failed_path_calculations')} path calculations in a row and "
                    f"found no way to this target.{where} Later collections in this task skip it; pass "
                    "retry_unreachable=true once the Body has a way to reach it."
                )
        return ToolResult(success, reason, can_retry=not success, next_suggestion=suggestion, metrics=metrics)

    while time.monotonic() < deadline and actions_used < config.mutation_budget:
        execution_checkpoint()
        # Checked before every native action: a pickaxe that broke during the
        # previous one is noticed here rather than mined past bare-handed.
        denied = harvest_tool_denial(body, blocks)
        if denied is not None:
            current = inventory_total()
            return ToolResult(False, denied.reason, can_retry=False, next_suggestion=denied.next_suggestion,
                metrics={**denied.metrics, "collected": current - before, "requested_count": remaining_count,
                         "remaining_count": max(0, remaining_count - (current - before)), "attempts": attempts,
                         "collection_strategy": "native_resource_collection"})
        current = inventory_total()
        finishing = current - before >= remaining_count
        if finishing and finish_target is None:
            return result(True, "resource_collected")
        needed = max(0, remaining_count - (current - before))
        timeout = min(300.0, deadline - time.monotonic())
        if timeout <= 0:
            break
        cluster: list[Position] = []
        anchor = finish_target if finishing else None
        finish_target = None
        if tree and not finishing:
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
        if anchor is not None:
            # Spend a little of the remaining task time finishing the current
            # cluster, without making a forest or mountain an obligation.
            # One extra block per second is a work estimate, not a resource-size
            # classification. Probe one beyond the allowance.
            finish_allowance = int(timeout * 0.1)
            probe_limit = min(257, needed + finish_allowance + 1)
            cluster = resource_cluster(body, anchor, blocks, probe_limit,
                tree=tree or all(b.endswith("_ore") for b in blocks), include_neighbors=finishing)
            cluster = [pos for pos in cluster if tuple(pos) not in avoided]
            if not cluster:
                if finishing:
                    return result(True, "resource_collected")
                excluded.add(anchor)
                continue
            if len(cluster) >= probe_limit or len(cluster) > 256:
                cluster = []
        if finishing and not cluster:
            return result(True, "resource_collected")
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
        held = inventory_total()
        terminal, stalled = _await_with_progress(
            body, action, inventory_total, lambda: harvest_tool_denial(body, blocks) is not None)
        attempts.append({"target": list(anchor) if anchor is not None else None,
                         "targets": [list(pos) for pos in cluster], "action": action.name,
                         "mine": terminal.to_payload(), **({"no_progress": stalled} if stalled else {})})
        actions_used += 1
        if stalled:
            if stalled.get("reason") == TOOL_LOST:
                continue  # the check at the top of the loop reports which tool is missing
            target = stalled.get("target")
            if unreachable is not None and stalled.get("reason") == "path_unreachable" and target:
                unreachable.add(tuple(target))
                avoided.add(tuple(target))
            return result(False, str(stalled["reason"]), stalled)
        if not terminal.success:
            # One cluster that did not finish is not the end of a collection
            # that is still producing: note it, leave it, take the next one.
            # The inventory decides when the request is met, not each cluster.
            gained = inventory_total() - held
            fruitless = 0 if gained > 0 else fruitless + 1
            # While only finishing a vein after the request is met there is no
            # next cluster to move on to, so that failure is the result.
            if not cluster or finishing or fruitless >= FRUITLESS_CLUSTER_LIMIT:
                return result(False, terminal.reason)
            unfinished.append({"target": list(anchor) if anchor is not None else None,
                               "reason": terminal.reason, "cells": len(cluster), "gained": gained})
            excluded.update(tuple(pos) for pos in cluster)
            if anchor is not None:
                excluded.add(anchor)
            continue
        fruitless = 0
        if action.name == "mineResource":
            selected = (terminal.metrics or {}).get("last_resource_target")
            if isinstance(selected, list) and len(selected) == 3:
                finish_target = tuple(int(value) for value in selected)
    complete = inventory_total() - before >= remaining_count
    return result(complete, "resource_collected" if complete else "resource_domain_budget_exhausted")
