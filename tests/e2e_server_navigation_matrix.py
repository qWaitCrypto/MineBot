#!/usr/bin/env python3
"""Live gate for the production Scarpet non-mutating movement graph."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body import NavigationRunConfig, NavigationTransactions  # noqa: E402
from minebot.game import GovernancePolicy, Region, ScarpetBody  # noqa: E402
from minebot.game.navigation import GoalComposite, GoalNear  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


BOT = "NavMatrix"
BASE_X = 320
BASE_Y = 120
BASE_Z = 320


def command(rcon, text: str, *, delay: float = 0.05) -> str:
    result = rcon.command(text)
    if delay:
        time.sleep(delay)
    return result


def clear_lane(rcon, x: int, z: int, *, x_size: int = 9, z_size: int = 5) -> None:
    command(rcon, f"fill {x - 1} {BASE_Y - 6} {z - z_size // 2} {x + x_size} {BASE_Y + 4} {z + z_size // 2} air")


def flat_floor(rcon, x: int, z: int, *, x_size: int = 9, z_radius: int = 2, y: int = BASE_Y - 1) -> None:
    command(rcon, f"fill {x - 1} {y} {z - z_radius} {x + x_size} {y} {z + z_radius} stone")


def teleport(rcon, pos: tuple[int, int, int]) -> None:
    command(rcon, f"player {BOT} stop")
    command(rcon, f"tp {BOT} {pos[0] + 0.5} {pos[1]} {pos[2] + 0.5}", delay=0.2)


def movement_total(result, kind: str) -> int:
    return sum(
        int((segment.get("diagnostics", {}).get("movement_counts") or {}).get(kind, 0))
        for segment in (result.metrics or {}).get("segments", [])
    )


def navigate(
    body: ScarpetBody,
    target,
    *,
    config: NavigationRunConfig | None = None,
    governance: GovernancePolicy | None = None,
):
    runtime = NavigationTransactions.server_side(body, governance or GovernancePolicy())
    return runtime.navigate_to(
        target,
        config=config or NavigationRunConfig(max_segments=5, segment_timeout_s=8.0, min_partial_progress=2),
    )


def setup_bridge_lane(rcon, x: int, z: int, *, back_length: int = 2) -> tuple[int, int, int]:
    x_min = x - back_length
    x_max = x + 6
    command(rcon, f"fill {x_min} {BASE_Y - 6} {z - 2} {x_max} {BASE_Y + 4} {z + 2} air")
    command(rcon, f"fill {x_min} {BASE_Y - 1} {z} {x} {BASE_Y - 1} {z} stone")
    command(rcon, f"fill {x + 2} {BASE_Y - 1} {z} {x_max} {BASE_Y - 1} {z} stone")
    command(rcon, f"fill {x_min} {BASE_Y} {z - 1} {x_max} {BASE_Y + 2} {z - 1} stone")
    command(rcon, f"fill {x_min} {BASE_Y} {z + 1} {x_max} {BASE_Y + 2} {z + 1} stone")
    return (x + 1, BASE_Y - 1, z)


def set_scaffold_inventory(rcon, count: int) -> None:
    command(rcon, f"clear {BOT}")
    command(rcon, f"script in minebot run inventory_set('{BOT}', 0, {count}, 'minecraft:cobblestone')")


def inventory_count(body: ScarpetBody, item: str) -> int:
    wanted = item.removeprefix("minecraft:")
    return sum(
        slot.count
        for slot in body.get_inventory()
        if not slot.empty and str(slot.item or "").removeprefix("minecraft:") == wanted
    )


def block_type(body: ScarpetBody, pos: tuple[int, int, int]) -> str:
    fact = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
    if not (fact.ok and fact.complete):
        raise AssertionError(f"block read failed at {pos}: {fact}")
    return str(fact.data.get("type") or "unknown").removeprefix("minecraft:")


def require_move(body: ScarpetBody, target: tuple[int, int, int], kind: str):
    result = navigate(body, target)
    if not result.success or movement_total(result, kind) < 1:
        raise AssertionError(f"{kind} gate failed: {result.to_payload()}")
    print(f"PASS {kind}: pos={body.get_state().pos} counts={movement_total(result, kind)}")


class WorldChangingBody(ScarpetBody):
    def __init__(self, *args, obstacle: tuple[int, int, int], **kwargs):
        super().__init__(*args, **kwargs)
        self.obstacle = obstacle
        self.injected = False

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs):
        if not self.injected:
            x, y, z = self.obstacle
            command(self.transport, f"setblock {x} {y} {z} stone", delay=0.0)
            command(self.transport, f"setblock {x} {y + 1} {z} stone", delay=0.0)
            self.injected = True
        return super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)


class InterruptingBody(ScarpetBody):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interrupted = False

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs):
        if not self.interrupted:
            accepted = self.interrupt("matrix_mid_fall")
            if not (accepted.ok and accepted.accepted):
                raise AssertionError(f"interrupt rejected: {accepted}")
            self.interrupted = True
        return super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)


class BridgeInterruptingBody(ScarpetBody):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interrupted = False

    def execute(self, action):
        result = super().execute(action)
        if (
            not self.interrupted
            and action.name == "navigationMutationDecision"
            and action.params.get("authorized") is True
            and result.ok
            and result.accepted
        ):
            time.sleep(0.12)
            accepted = self.interrupt("matrix_mid_bridge")
            if not (accepted.ok and accepted.accepted):
                raise AssertionError(f"bridge interrupt rejected: {accepted}")
            self.interrupted = True
        return result


def main() -> None:
    with connect_or_skip() as rcon:
        for text in (
            "script unload minebot",
            "script load minebot global",
            "script in minebot run minebot_reset()",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule doMobSpawning false",
            f"player {BOT} kill",
        ):
            command(rcon, text)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (BASE_X, BASE_Y, BASE_Z))
        command(rcon, f"gamemode survival {BOT}")
        command(rcon, f"clear {BOT}")

        x, z = BASE_X, BASE_Z
        clear_lane(rcon, x, z, x_size=8, z_size=9)
        flat_floor(rcon, x, z, x_size=8, z_radius=4)
        teleport(rcon, (x, BASE_Y, z))
        require_move(body, (x + 4, BASE_Y, z + 4), "diagonal")

        x, z = BASE_X + 20, BASE_Z
        clear_lane(rcon, x, z)
        flat_floor(rcon, x, z)
        command(rcon, f"fill {x + 2} {BASE_Y} {z - 2} {x + 8} {BASE_Y} {z + 2} stone")
        teleport(rcon, (x, BASE_Y, z))
        require_move(body, (x + 5, BASE_Y + 1, z), "ascend")
        teleport(rcon, (x + 5, BASE_Y + 1, z))
        require_move(body, (x, BASE_Y, z), "descend")

        x, z = BASE_X + 40, BASE_Z
        clear_lane(rcon, x, z)
        command(rcon, f"setblock {x} {BASE_Y - 1} {z} stone")
        command(rcon, f"fill {x + 1} {BASE_Y - 4} {z} {x + 5} {BASE_Y - 4} {z} stone")
        teleport(rcon, (x, BASE_Y, z))
        require_move(body, (x + 3, BASE_Y - 3, z), "fall")

        x, z = BASE_X + 50, BASE_Z
        clear_lane(rcon, x, z)
        command(rcon, f"setblock {x} {BASE_Y - 1} {z} stone")
        command(rcon, f"fill {x + 1} {BASE_Y - 4} {z} {x + 5} {BASE_Y - 4} {z} stone")
        teleport(rcon, (x, BASE_Y, z))
        interrupting_body = InterruptingBody(BOT, rcon)
        interrupted = navigate(interrupting_body, (x + 3, BASE_Y - 3, z))
        interrupted_y = interrupting_body.get_state().pos[1]
        if interrupted.success or interrupted.reason != "interrupted" or interrupted_y > BASE_Y - 2.5:
            raise AssertionError(
                f"mid-fall interrupt did not land before terminal: result={interrupted.to_payload()} y={interrupted_y}"
            )
        print(f"PASS mid_fall_interrupt: reason={interrupted.reason} y={interrupted_y}")

        x, z = BASE_X + 60, BASE_Z
        clear_lane(rcon, x, z)
        command(rcon, f"setblock {x} {BASE_Y - 1} {z} stone")
        command(rcon, f"fill {x + 1} {BASE_Y - 5} {z} {x + 4} {BASE_Y - 5} {z} stone")
        teleport(rcon, (x, BASE_Y, z))
        unsafe = navigate(body, (x + 2, BASE_Y - 4, z))
        if unsafe.success or unsafe.reason not in {"no_path", "budget_exceeded"}:
            raise AssertionError(f"unsafe fall was accepted: {unsafe.to_payload()}")
        if body.get_state().pos[1] < BASE_Y - 0.5:
            raise AssertionError(f"unsafe fall moved the bot: {body.get_state().pos}")
        print(f"PASS unsafe_fall: reason={unsafe.reason}")

        x, z = BASE_X, BASE_Z + 20
        clear_lane(rcon, x, z)
        flat_floor(rcon, x, z)
        command(rcon, f"fill {x + 2} {BASE_Y} {z} {x + 4} {BASE_Y} {z} water")
        teleport(rcon, (x, BASE_Y, z))
        require_move(body, (x + 7, BASE_Y, z), "swim")

        for offset, block in (
            (20, "oak_slab[type=bottom]"),
            (40, "oak_stairs[facing=east,half=bottom,shape=straight]"),
        ):
            x, z = BASE_X + offset, BASE_Z + 20
            clear_lane(rcon, x, z, x_size=7, z_size=3)
            flat_floor(rcon, x, z, x_size=7, z_radius=0)
            command(rcon, f"fill {x - 1} {BASE_Y} {z - 1} {x + 7} {BASE_Y + 2} {z - 1} stone")
            command(rcon, f"fill {x - 1} {BASE_Y} {z + 1} {x + 7} {BASE_Y + 2} {z + 1} stone")
            command(rcon, f"setblock {x + 2} {BASE_Y} {z} {block}")
            teleport(rcon, (x, BASE_Y, z))
            result = navigate(body, (x + 5, BASE_Y, z))
            label = "slab" if "slab" in block else "stairs"
            if not result.success:
                raise AssertionError(f"{label} gate failed: {result.to_payload()}")
            print(f"PASS {label}: pos={body.get_state().pos}")

        x, z = BASE_X + 60, BASE_Z + 20
        clear_lane(rcon, x, z)
        flat_floor(rcon, x, z)
        teleport(rcon, (x, BASE_Y, z))
        changing_body = WorldChangingBody(BOT, rcon, obstacle=(x + 3, BASE_Y, z))
        changed = navigate(changing_body, (x + 7, BASE_Y, z))
        reasons = [segment["terminal_reason"] for segment in (changed.metrics or {}).get("segments", [])]
        if not changed.success or "world_changed" not in reasons or reasons[-1] != "arrived":
            raise AssertionError(f"world-change replan failed: {changed.to_payload()}")
        print(f"PASS world_change_replan: reasons={reasons}")

        x, z = BASE_X, BASE_Z + 40
        bridge_pos = setup_bridge_lane(rcon, x, z)
        teleport(rcon, (x, BASE_Y, z))
        set_scaffold_inventory(rcon, 4)
        bridge_policy = GovernancePolicy(
            natural_regions=[Region("bridge-lane", (x - 2, BASE_Y - 8, z - 2), (x + 6, BASE_Y + 5, z + 2))]
        )
        before_scaffold = inventory_count(body, "cobblestone")
        bridged = navigate(
            body,
            (x + 5, BASE_Y, z),
            governance=bridge_policy,
            config=NavigationRunConfig(
                max_segments=6,
                segment_timeout_s=8.0,
                min_partial_progress=2,
                max_place_steps=2,
            ),
        )
        after_scaffold = inventory_count(body, "cobblestone")
        bridge_reasons = [segment["terminal_reason"] for segment in (bridged.metrics or {}).get("segments", [])]
        if not bridged.success or bridge_reasons[-1:] != ["arrived"] or "world_changed" not in bridge_reasons:
            raise AssertionError(f"bridge gate failed: {bridged.to_payload()}")
        if block_type(body, bridge_pos) != "cobblestone":
            raise AssertionError(f"bridge world fact missing at {bridge_pos}: {block_type(body, bridge_pos)}")
        if before_scaffold - after_scaffold != 1:
            raise AssertionError(f"bridge inventory delta wrong: before={before_scaffold} after={after_scaffold}")
        placement = bridge_policy.bot_placements.get(bridge_pos)
        if placement is None or placement.purpose != "bridge" or placement.bot != BOT:
            raise AssertionError(f"bridge ledger missing: {bridge_policy.bot_placements}")
        print(f"PASS bridge: pos={body.get_state().pos} reasons={bridge_reasons} inventory={before_scaffold}->{after_scaffold}")

        x, z = BASE_X + 40, BASE_Z + 40
        denied_pos = setup_bridge_lane(rcon, x, z, back_length=18)
        teleport(rcon, (x, BASE_Y, z))
        set_scaffold_inventory(rcon, 4)
        denied_policy = GovernancePolicy(
            natural_regions=[Region("any-of-lane", (x - 18, BASE_Y - 8, z - 2), (x + 6, BASE_Y + 5, z + 2))],
            protected_regions=[Region("protected-bridge", denied_pos, denied_pos)],
        )
        before_denied = inventory_count(body, "cobblestone")
        far_goal = (x - 16, BASE_Y, z)
        switched = navigate(
            body,
            GoalComposite((GoalNear((x + 4, BASE_Y, z), radius=0), GoalNear(far_goal, radius=0))),
            governance=denied_policy,
            config=NavigationRunConfig(
                max_segments=6,
                segment_timeout_s=8.0,
                min_partial_progress=2,
                max_place_steps=2,
            ),
        )
        after_denied = inventory_count(body, "cobblestone")
        switched_reasons = [segment["terminal_reason"] for segment in (switched.metrics or {}).get("segments", [])]
        if not switched.success or switched.metrics.get("selected_goal") != list(far_goal):
            raise AssertionError(f"denied any-of did not select far goal: {switched.to_payload()}")
        if "mutation_denied" not in switched_reasons or switched_reasons[-1:] != ["arrived"]:
            raise AssertionError(f"denied any-of terminal chain wrong: {switched.to_payload()}")
        if block_type(body, denied_pos) != "air" or before_denied != after_denied:
            raise AssertionError(
                f"protected bridge mutated world: block={block_type(body, denied_pos)} inventory={before_denied}->{after_denied}"
            )
        if denied_policy.bot_placements:
            raise AssertionError(f"protected bridge entered ledger: {denied_policy.bot_placements}")
        print(f"PASS bridge_denied_any_of: selected={far_goal} reasons={switched_reasons} inventory={before_denied}")

        x, z = BASE_X + 70, BASE_Z + 40
        interrupted_bridge_pos = setup_bridge_lane(rcon, x, z)
        teleport(rcon, (x, BASE_Y, z))
        set_scaffold_inventory(rcon, 4)
        interrupt_policy = GovernancePolicy(
            natural_regions=[Region("interrupt-bridge", (x - 2, BASE_Y - 8, z - 2), (x + 6, BASE_Y + 5, z + 2))]
        )
        interrupting_bridge_body = BridgeInterruptingBody(BOT, rcon)
        interrupting_bridge_body.last_seq = body.last_seq
        before_interrupt = inventory_count(interrupting_bridge_body, "cobblestone")
        interrupted_bridge = navigate(
            interrupting_bridge_body,
            (x + 5, BASE_Y, z),
            governance=interrupt_policy,
            config=NavigationRunConfig(
                max_segments=4,
                segment_timeout_s=8.0,
                min_partial_progress=2,
                max_place_steps=2,
            ),
        )
        after_interrupt = inventory_count(interrupting_bridge_body, "cobblestone")
        interrupted_state = interrupting_bridge_body.get_state()
        on_ground = "1b" in command(rcon, f"data get entity {BOT} OnGround", delay=0.0)
        if interrupted_bridge.success or interrupted_bridge.reason != "interrupted":
            raise AssertionError(f"mid-bridge interrupt terminal wrong: {interrupted_bridge.to_payload()}")
        if block_type(interrupting_bridge_body, interrupted_bridge_pos) != "cobblestone":
            raise AssertionError(f"mid-bridge interrupt did not finish placement: {interrupted_bridge.to_payload()}")
        if before_interrupt - after_interrupt != 1 or interrupted_bridge_pos not in interrupt_policy.bot_placements:
            raise AssertionError(
                f"mid-bridge interrupt lost inventory/ledger truth: inventory={before_interrupt}->{after_interrupt} "
                f"ledger={interrupt_policy.bot_placements}"
            )
        if not on_ground or abs(interrupted_state.pos[1] - BASE_Y) > 0.05:
            raise AssertionError(f"mid-bridge interrupt returned before safe landing: {interrupted_state}")
        print(
            f"PASS mid_bridge_interrupt: reason={interrupted_bridge.reason} pos={interrupted_state.pos} "
            f"inventory={before_interrupt}->{after_interrupt}"
        )

        command(rcon, f"player {BOT} kill")
        print("SERVER NAVIGATION N2/N3 BRIDGE MATRIX PASSED")


if __name__ == "__main__":
    main()
