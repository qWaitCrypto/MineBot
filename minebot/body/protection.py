"""Small protection actions that compose existing guarded Body transactions."""

from __future__ import annotations

from math import isfinite

from minebot.body.block_work import BlockWork, _state_block_pos
from minebot.contract import Action, ToolResult, action_wait_timeout_s, action_with_timeout, terminal_event_to_tool_result


class ProtectionTransactions:
    def __init__(self, work: BlockWork, inventory=None, objective_body=None) -> None:
        self.work = work
        self.inventory = inventory
        # The client-side timed pillar controller, independent of Baritone.
        self.objective_body = objective_body

    def pillar_up(
        self, *, blocks: int = 3,
        scaffold_blocks: tuple[str, ...] | list[str] = BlockWork.DEFAULT_PILLAR_BLOCKS,
        timeout_s: float = 30.0,
    ) -> ToolResult:
        if type(blocks) is not int or not 1 <= blocks <= 16:
            raise ValueError("blocks must be an integer from 1 to 16")
        if type(timeout_s) not in (int, float) or not isfinite(timeout_s) or not 0 < timeout_s <= 120:
            raise ValueError("timeout_s must be > 0 and <= 120")
        state = self.work.body.get_state()
        origin = _state_block_pos(state.pos)
        counts = getattr(state, "inventory_counts", None)
        if counts is not None:
            wanted = {str(item).removeprefix("minecraft:") for item in scaffold_blocks}
            held = sum(count for item, count in counts.items() if str(item).removeprefix("minecraft:") in wanted)
            if held < blocks:
                return ToolResult(False, "not_enough_blocks", False, metrics={
                    "origin": list(origin), "position": list(origin), "blocks_requested": blocks,
                    "blocks_raised": 0, "blocks_needed": blocks, "blocks_held": held,
                })
        if self.objective_body is not None:
            from minebot.body.inventory import _dispatch

            result = _dispatch(self.objective_body, "pillarUp", {
                "blocks": blocks,
                "scaffold_blocks": list(scaffold_blocks), "timeout_ticks": int(timeout_s * 20),
            }, timeout_s=float(timeout_s) + 5.0)
        else:
            result = self.work.dig_up_to_y(
                origin[1] + blocks, current_pos=origin, scaffold_blocks=scaffold_blocks,
                max_steps=blocks, timeout_s=float(timeout_s),
            )
        observed = _state_block_pos(self.work.body.get_state().pos)
        facts = {**dict(result.metrics or {}), "origin": list(origin), "position": list(observed),
                 "blocks_requested": blocks, "blocks_raised": max(0, observed[1] - origin[1])}
        if observed[0] != origin[0] or observed[2] != origin[2]:
            return ToolResult(False, "displaced", False, metrics=facts)
        if result.success and observed[1] - origin[1] == blocks:
            return ToolResult(True, "pillar_height_reached", False, metrics=facts)
        # Only the missing-material case is renamed; every other reason is the
        # step's own observed reason, never a guessed "blocked above".
        reason = "not_enough_blocks" if result.reason in {"dig_up_no_scaffold_available", "not_enough_blocks"} else result.reason
        return ToolResult(False, reason, result.can_retry, metrics=facts)

    def raise_shield(self, *, target: str, duration_ticks: int = 100) -> ToolResult:
        if not isinstance(target, str) or not target.strip():
            raise ValueError("target must be a non-empty entity UUID")
        if type(duration_ticks) is not int or not 1 <= duration_ticks <= 1200:
            raise ValueError("duration_ticks must be an integer from 1 to 1200")
        from minebot.body.combat import resolve_entity_target

        resolved = resolve_entity_target(self.work.body, target, radius=32)
        if isinstance(resolved, ToolResult):
            return resolved

        equipped_offhand = False
        if self.inventory is not None:
            equipped = self.inventory.equip_item(item="shield", target="offhand")
            if not equipped.success:
                if equipped.reason == "item_not_available":
                    return ToolResult(False, "no_shield", False, metrics={"equipped_offhand": False})
                return equipped
            equipped_offhand = equipped.reason != "already_equipped"
        action = action_with_timeout(Action.create("raiseShield", {
            "target_uuid": resolved.target_uuid, "duration_ticks": duration_ticks,
        }), max(10.0, duration_ticks / 20 + 10))
        accepted = self.work.body.execute(action)
        if not (accepted.ok and accepted.accepted):
            from minebot.contract import body_rejection_to_tool_result
            result = body_rejection_to_tool_result(accepted, {**resolved.evidence(), "equipped_offhand": equipped_offhand})
            if result is not None:
                return result
            return ToolResult(False, "body_rejected", True,
                              metrics={**resolved.evidence(), "equipped_offhand": equipped_offhand})
        terminal = self.work.body.await_action_terminal(action.id, timeout_s=action_wait_timeout_s(action))
        result = terminal_event_to_tool_result(terminal)
        return ToolResult(result.success, result.reason, result.can_retry, result.next_suggestion,
                          {**dict(result.metrics or {}), **resolved.evidence(),
                           "equipped_offhand": equipped_offhand})

    def place_bed_and_sleep(self, *, timeout_s: float = 300.0) -> ToolResult:
        if type(timeout_s) not in (int, float) or not isfinite(timeout_s) or not 0 < timeout_s <= 600:
            raise ValueError("timeout_s must be > 0 and <= 600")
        action = action_with_timeout(Action.create("placeBedAndSleep", {}), float(timeout_s))
        accepted = self.work.body.execute(action)
        if not (accepted.ok and accepted.accepted):
            from minebot.contract import body_rejection_to_tool_result
            return body_rejection_to_tool_result(accepted)
        terminal = self.work.body.await_action_terminal(action.id, timeout_s=action_wait_timeout_s(action))
        return terminal_event_to_tool_result(terminal)
