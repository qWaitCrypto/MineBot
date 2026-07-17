#!/usr/bin/env python3
"""Fixed-fixture live matrix for structure-risk break governance."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import BlockWork, VoxelStructureRiskAssessor
from minebot.contract import BreakContext
from minebot.game import GovernancePolicy, Region, ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "E2EStructRisk"
TARGET = (173, 70, 0)
REGION = Region("structure-risk", (166, 60, -5), (180, 80, 5))


def command(rcon, command_text: str, delay: float = 0.05) -> str:
    output = rcon.command(command_text)
    if delay:
        time.sleep(delay)
    return output


def setup_world(rcon) -> None:
    for command_text in (
        "script unload minebot",
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        f"player {BOT} kill",
        "fill 166 66 -5 180 75 5 air",
        "fill 166 69 -5 180 69 5 stone",
        "script in minebot run minebot_reset()",
    ):
        command(rcon, command_text)


def reset_case(rcon, *, blocks: list[tuple[int, int, int, str]], tool: str) -> None:
    for command_text in (
        "fill 166 66 -5 180 75 5 air",
        "fill 166 69 -5 180 69 5 stone",
        f"tp {BOT} 171 70 0 -90 0",
        f"gamemode survival {BOT}",
        f"effect clear {BOT}",
        f"item replace entity {BOT} weapon.mainhand with {tool}",
        "script in minebot run minebot_reset()",
    ):
        command(rcon, command_text)
    for x, y, z, block_type in blocks:
        command(rcon, f"setblock {x} {y} {z} {block_type}", delay=0.0)
    time.sleep(0.1)


def policy_for(body: ScarpetBody) -> GovernancePolicy:
    return GovernancePolicy(
        natural_regions=[REGION],
        structure_risk_assessor=VoxelStructureRiskAssessor(body),
        require_structure_assessment=True,
    )


def block_type(body: ScarpetBody) -> str:
    perception = body.perceive(
        "blockAt",
        {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]},
    )
    if not perception.ok or not perception.complete:
        raise AssertionError(f"target read failed: {perception}")
    return str(perception.data.get("type") or "unknown").removeprefix("minecraft:")


def run_case(
    rcon,
    body: ScarpetBody,
    *,
    name: str,
    target_type: str,
    context: BreakContext,
    blocks: list[tuple[int, int, int, str]],
    tool: str,
    expected_success: bool,
    expected_reason: str | None = None,
) -> dict[str, object]:
    reset_case(rcon, blocks=blocks, tool=tool)
    result = BlockWork(body, policy_for(body)).mine_block(
        TARGET,
        context=context,
        approach=False,
        timeout_s=15.0,
    )
    after = block_type(body)
    payload = result.to_payload()
    if result.success != expected_success:
        raise AssertionError(f"{name} success mismatch: {payload}, after={after}")
    if expected_reason is not None and result.reason != expected_reason:
        raise AssertionError(f"{name} reason mismatch: {payload}")
    if expected_success and after not in {"air", "cave_air", "void_air"}:
        raise AssertionError(f"{name} allowed break did not clear target: {payload}, after={after}")
    if not expected_success and after != target_type:
        raise AssertionError(f"{name} denied break mutated target: {payload}, after={after}")
    legality = dict((result.metrics or {}).get("legality") or {})
    risk = dict(legality.get("details") or {}).get("structure_risk")
    if not isinstance(risk, dict) or risk.get("complete") is not True:
        raise AssertionError(f"{name} missing complete structure-risk truth: {payload}")
    return {
        "success": result.success,
        "reason": result.reason,
        "after": after,
        "risk": risk,
    }


def main() -> None:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (170, 70, 0))
        cases = {
            "natural_tree": run_case(
                rcon,
                body,
                name="natural_tree",
                target_type="oak_log",
                context=BreakContext.COLLECT,
                blocks=[
                    (173, 69, 0, "dirt"),
                    (173, 70, 0, "oak_log"),
                    (173, 71, 0, "oak_log"),
                    (173, 72, 0, "oak_log"),
                    (173, 73, 0, "oak_log"),
                    (172, 73, 0, "oak_leaves"),
                    (174, 73, 0, "oak_leaves"),
                    (173, 73, 1, "oak_leaves"),
                ],
                tool="diamond_axe",
                expected_success=True,
            ),
            "natural_ore": run_case(
                rcon,
                body,
                name="natural_ore",
                target_type="coal_ore",
                context=BreakContext.COLLECT,
                blocks=[
                    (*TARGET, "coal_ore"),
                    (173, 70, 1, "coal_ore"),
                    (174, 70, 0, "stone"),
                    (173, 71, 0, "stone"),
                ],
                tool="diamond_pickaxe",
                expected_success=True,
            ),
            "natural_irregular_stone": run_case(
                rcon,
                body,
                name="natural_irregular_stone",
                target_type="stone",
                context=BreakContext.DIRECT,
                blocks=[
                    (*TARGET, "stone"),
                    (173, 70, 1, "andesite"),
                    (174, 70, 0, "dirt"),
                    (173, 71, 0, "stone"),
                ],
                tool="diamond_pickaxe",
                expected_success=True,
            ),
            "built_manufactured_neighbor": run_case(
                rcon,
                body,
                name="built_manufactured_neighbor",
                target_type="stone",
                context=BreakContext.DIRECT,
                blocks=[(*TARGET, "stone"), (173, 70, 1, "oak_planks")],
                tool="diamond_pickaxe",
                expected_success=False,
                expected_reason="break_denied:player_structure_risk",
            ),
            "built_horizontal_log": run_case(
                rcon,
                body,
                name="built_horizontal_log",
                target_type="oak_log",
                context=BreakContext.COLLECT,
                blocks=[
                    (172, 70, 0, "oak_log"),
                    (*TARGET, "oak_log"),
                    (174, 70, 0, "oak_log"),
                    (175, 70, 0, "oak_log"),
                ],
                tool="diamond_axe",
                expected_success=False,
                expected_reason="break_denied:structure_risk_unknown",
            ),
            "built_stone_wall": run_case(
                rcon,
                body,
                name="built_stone_wall",
                target_type="stone",
                context=BreakContext.TRAVEL,
                blocks=[
                    (173, y, z, "stone")
                    for y in range(67, 74)
                    for z in range(-1, 2)
                ],
                tool="diamond_pickaxe",
                expected_success=False,
                expected_reason="break_denied:structure_risk_unknown",
            ),
            "built_cobblestone_pillar": run_case(
                rcon,
                body,
                name="built_cobblestone_pillar",
                target_type="cobblestone",
                context=BreakContext.RECOVERY,
                blocks=[
                    (173, y, 0, "cobblestone")
                    for y in range(69, 74)
                ],
                tool="diamond_pickaxe",
                expected_success=False,
                expected_reason="break_denied:player_structure_risk",
            ),
        }
        print(json.dumps(cases, sort_keys=True))


if __name__ == "__main__":
    main()
