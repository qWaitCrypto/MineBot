#!/usr/bin/env python3
"""Bounded no-model wood-to-stone-to-iron rehearsal from natural terrain."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.brain.composition import CompositionBudget
from minebot.contract import Region, ToolResult, governance_confirmation_from
from minebot.game.rcon import RconClient, RconConfig


BOT = "JavaNaturalProg"
BODY_URL = "ws://127.0.0.1:8767"
GOLDEN_NATURAL_START_BLOCK = (-47, 73, -28)
NATURAL = Region("formal-world", (-256, -128, -256), (256, 320, 256))
ARTIFACT = Path(
    "logs/agentic-runtime/java-body-natural-resource-progression-20260805.json"
)


def wait_for_safe_natural_spawn(body, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    previous: tuple[float, float, float] | None = None
    stable_reads = 0
    while time.monotonic() < deadline:
        state = body.get_state()
        if state.missing or state.hazard_unresolved is not None:
            previous = None
            stable_reads = 0
            time.sleep(0.1)
            continue
        current = tuple(float(value) for value in state.pos)
        if previous is not None and math.dist(previous, current) <= 0.02:
            stable_reads += 1
        else:
            stable_reads = 0
        previous = current
        if stable_reads < 3:
            time.sleep(0.1)
            continue

        x = math.floor(current[0])
        y = math.floor(current[1] + 1.0e-4)
        z = math.floor(current[2])
        cells = body.perceive(
            "blockCells",
            {
                "cells": [[x, y - 1, z], [x, y, z], [x, y + 1, z]],
                "start": 0,
                "limit": 3,
            },
        )
        observed = {
            (int(cell["x"]), int(cell["y"]), int(cell["z"])): dict(cell)
            for cell in cells.data.get("cells", [])
        } if cells.ok and cells.complete else {}
        support = observed.get((x, y - 1, z), {})
        feet = observed.get((x, y, z), {})
        head = observed.get((x, y + 1, z), {})
        evidence = {
            "pos": list(current),
            "support": support,
            "feet": feet,
            "head": head,
        }
        if (
            support.get("state") == "SOLID"
            and feet.get("state") in {"CLEAR", "PASSABLE"}
            and head.get("state") in {"CLEAR", "PASSABLE"}
            and state.body_owner is None
            and state.pending_action_count == 0
        ):
            return state, evidence
        raise AssertionError(f"server-selected natural spawn is not a safe stand: {evidence}")
    raise AssertionError(f"{BOT} did not settle at a safe natural spawn: {body.get_state()}")


def event_payload(event) -> dict[str, object]:
    return {
        "seq": event.seq,
        "tick": event.tick,
        "name": event.name,
        "data": dict(event.data),
    }


def completed_steps(result) -> list[dict[str, object]]:
    return list((result.metrics or {}).get("completed_steps") or [])


def step_summary(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "kind": (entry.get("step") or {}).get("kind"),
            "item": (entry.get("step") or {}).get("item"),
            "reason": (entry.get("result") or {}).get("reason"),
        }
        for entry in entries
    ]


def item_count(counts: dict[str, int], item: str) -> int:
    return sum(
        int(count)
        for raw_item, count in counts.items()
        if str(raw_item).removeprefix("minecraft:") == item
    )


def discovered_blocks(result: ToolResult) -> list[dict[str, object]]:
    return list((result.metrics or {}).get("blocks") or [])


def call_with_operator_confirmations(
    parts,
    tool_name: str,
    params: dict[str, object],
    *,
    purpose: str,
    max_attempts: int = 8,
) -> tuple[ToolResult, list[dict[str, object]], list[dict[str, object]]]:
    attempts: list[dict[str, object]] = []
    confirmations: list[dict[str, object]] = []
    seen_challenges: set[str] = set()
    result = ToolResult(False, "bounded_rehearsal_not_started", False)
    for _ in range(max_attempts):
        result = parts.registry.get(tool_name).callable(dict(params))
        challenge = governance_confirmation_from(result.to_payload())
        attempts.append(
            {
                "success": result.success,
                "reason": result.reason,
                "challenge_id": challenge.get("challenge_id") if challenge else None,
            }
        )
        if result.success or challenge is None:
            break

        challenge_id = str(challenge.get("challenge_id") or "")
        evidence = challenge.get("evidence")
        reviewable = (
            challenge_id
            and challenge_id not in seen_challenges
            and isinstance(evidence, dict)
            and evidence.get("complete") is True
            and evidence.get("level") == "ambiguous"
        )
        if not reviewable:
            break
        seen_challenges.add(challenge_id)
        confirmed = parts.registry.get("confirm_governance_scope").callable(
            {"challenge_id": challenge_id, "purpose": purpose}
        )
        confirmations.append(
            {
                "tool": tool_name,
                "challenge": challenge,
                "result": confirmed.to_payload(),
            }
        )
        if not confirmed.success:
            break
    return result, attempts, confirmations


def main() -> int:
    provider = build_body_provider(
        "java",
        bot_name=BOT,
        natural_region=NATURAL,
        java_body_url=BODY_URL,
    )
    body = provider.body
    parts = build_phase1_agent_runtime(
        body=body,
        goal_text="acquire and equip an iron pickaxe from natural spawn",
        model_provider=None,
        config=Phase1RuntimeConfig(
            natural_region=NATURAL,
            body_provider="java",
            governance_policy=provider.governance,
            budget=CompositionBudget(
                max_candidates=32,
                max_mutating_calls=48,
                max_wall_s=300.0,
            ),
        ),
        agent_name="JavaNaturalResourceProgression",
    )

    started = time.monotonic()
    milestones: list[dict[str, object]] = []
    governance_confirmations: list[dict[str, object]] = []
    call_attempts: dict[str, list[dict[str, object]]] = {}
    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        before_setup = body.get_state()
        if not before_setup.missing:
            body.interrupt("bounded_natural_rehearsal_reset")
            despawned = body.despawn()
            if not despawned.ok:
                raise AssertionError(f"Java Body could not reset natural player: {despawned}")
        spawned = body.spawn(
            GOLDEN_NATURAL_START_BLOCK,
            gamemode="survival",
            timeout_s=10.0,
        )
        if not spawned.ok:
            raise AssertionError(f"Java Body could not spawn natural player: {spawned}")
        rcon.command(f"clear {BOT}")
        rcon.command(f"effect clear {BOT}")
        rcon.command(f"gamemode survival {BOT}")
        before, spawn_evidence = wait_for_safe_natural_spawn(body, 5.0)

        start_seq = int(body.event_head("java-natural-resource-progression")["event_seq"])
        body.last_seq = start_seq
        try:
            stone_started = time.monotonic()
            stone_result, stone_attempts, stone_confirmations = call_with_operator_confirmations(
                parts,
                "ensure_tool_for",
                {"resource": "stone_pickaxe"},
                purpose="continue bounded natural-resource work in the golden test world",
            )
            call_attempts["stone_pickaxe"] = stone_attempts
            governance_confirmations.extend(stone_confirmations)
            stone_elapsed = time.monotonic() - stone_started
            stone_state = body.get_state()
            milestones.append(
                {
                    "name": "stone_pickaxe_equipped",
                    "elapsed_wall_s": round(time.monotonic() - started, 3),
                    "call_wall_s": round(stone_elapsed, 3),
                    "success": stone_result.success,
                    "reason": stone_result.reason,
                    "inventory": stone_state.inventory_counts or {},
                    "selected_item": stone_state.selected_item,
                }
            )

            iron_started = time.monotonic()
            iron_result, iron_attempts, iron_confirmations = call_with_operator_confirmations(
                parts,
                "ensure_tool_for",
                {"resource": "iron_pickaxe"},
                purpose="continue bounded natural-resource work in the golden test world",
            )
            call_attempts["iron_pickaxe"] = iron_attempts
            governance_confirmations.extend(iron_confirmations)
            iron_exploration = ToolResult(True, "not_needed", False)
            if not iron_result.success and iron_result.reason.endswith("target_not_found"):
                iron_explore_started = time.monotonic()
                (
                    iron_exploration,
                    iron_explore_attempts,
                    iron_explore_confirmations,
                ) = call_with_operator_confirmations(
                    parts,
                    "explore_for",
                    {
                        "block_targets": ["iron_ore", "deepslate_iron_ore"],
                        "max_distance": 192,
                        "max_regions": 12,
                        "return_policy": "first_match",
                        "scan_radius": 24,
                    },
                    purpose="continue bounded iron exploration in the golden test world",
                )
                call_attempts["explore_iron"] = iron_explore_attempts
                governance_confirmations.extend(iron_explore_confirmations)
                iron_found = bool(discovered_blocks(iron_exploration))
                milestones.append(
                    {
                        "name": "iron_discovered",
                        "elapsed_wall_s": round(time.monotonic() - started, 3),
                        "call_wall_s": round(time.monotonic() - iron_explore_started, 3),
                        "success": iron_found,
                        "reason": iron_exploration.reason,
                    }
                )
                if iron_found:
                    (
                        iron_result,
                        iron_retry_attempts,
                        iron_retry_confirmations,
                    ) = call_with_operator_confirmations(
                        parts,
                        "ensure_tool_for",
                        {"resource": "iron_pickaxe"},
                        purpose="continue bounded natural-resource work in the golden test world",
                    )
                    call_attempts["iron_pickaxe"].extend(iron_retry_attempts)
                    governance_confirmations.extend(iron_retry_confirmations)
            iron_elapsed = time.monotonic() - iron_started
            after = body.get_state()
            milestones.append(
                {
                    "name": "iron_pickaxe_equipped",
                    "elapsed_wall_s": round(time.monotonic() - started, 3),
                    "call_wall_s": round(iron_elapsed, 3),
                    "success": iron_result.success,
                    "reason": iron_result.reason,
                    "inventory": after.inventory_counts or {},
                    "selected_item": after.selected_item,
                }
            )
            body.poll_events()
        finally:
            body.interrupt("bounded_natural_rehearsal_cleanup")
            parts.runtime.close()
            body.despawn()

    elapsed = time.monotonic() - started
    events = [event for event in body.event_log if event.seq > start_seq]
    designated_targets = [event for event in events if event.name == "target_committed"]
    forbidden_strategy_events = [
        event.name
        for event in events
        if event.name.startswith("collection_candidate_")
        or event.name.startswith("collection_frontier_")
        or event.name == "collection_objective_domain_planned"
    ]
    inventory = after.inventory_counts or {}
    stone_steps = completed_steps(stone_result)
    iron_steps = completed_steps(iron_result)
    steps = step_summary([*stone_steps, *iron_steps])
    first_output_s = float(milestones[0]["elapsed_wall_s"]) if stone_result.success else None
    max_no_output_window_s = max(
        first_output_s or elapsed,
        iron_elapsed,
    )
    success = bool(
        stone_result.success
        and iron_result.success
        and item_count(inventory, "stone_pickaxe") >= 1
        and item_count(inventory, "iron_pickaxe") >= 1
        and str(after.selected_item or "").removeprefix("minecraft:") == "iron_pickaxe"
        and designated_targets
        and not forbidden_strategy_events
        and first_output_s is not None
        and first_output_s < 342.0
        and max_no_output_window_s < 900.0
    )
    artifact = {
        "scope": "java_body_natural_resource_progression",
        "formal_gate": False,
        "bounded": True,
        "production_provider": "java",
        "scarpet_constructed": provider.scarpet_body is not None,
        "rcon_role": "player_reset_and_verification_only",
        "terrain_fixture_commands": 0,
        "model_calls": 0,
        "strategy_calls": int("explore_iron" in call_attempts),
        "high_level_calls": sum(len(attempts) for attempts in call_attempts.values()),
        "operator_confirmation_calls": len(governance_confirmations),
        "governance_confirmations": governance_confirmations,
        "call_attempts": call_attempts,
        "natural_spawn": list(before.pos),
        "natural_spawn_source": "golden_natural_fixture_start",
        "natural_spawn_evidence": spawn_evidence,
        "start_pos": list(before.pos),
        "end_pos": list(after.pos),
        "elapsed_wall_s": round(elapsed, 3),
        "first_output_s": first_output_s,
        "max_no_output_window_s": round(max_no_output_window_s, 3),
        "milestones": milestones,
        "steps": steps,
        "designated_targets": [event_payload(event) for event in designated_targets],
        "forbidden_java_strategy_events": forbidden_strategy_events,
        "inventory": inventory,
        "selected_item": after.selected_item,
        "results": {
            "stone_pickaxe": stone_result.to_payload(),
            "explore_iron": iron_exploration.to_payload(),
            "iron_pickaxe": iron_result.to_payload(),
        },
        "success": success,
    }
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
