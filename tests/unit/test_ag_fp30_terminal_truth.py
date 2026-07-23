import unittest

from minebot.app.real_server_session import (
    AG_FP30_GOAL,
    evaluate_ag_fp30_terminal_truth,
    evaluate_terminal_truth,
    is_ag_fp30_goal,
)
from minebot.app.session import SessionStep
from minebot.brain.lifecycle import LifecycleState
from minebot.contract import BodyState, PerceptionResult


def _state(selected_slot=7):
    return BodyState(
        bot="Bot",
        pos=(0.5, 64.0, 0.5),
        yaw=None,
        pitch=None,
        health=20.0,
        food=20,
        oxygen=300,
        inventory_raw="",
        inventory_hash="terminal-truth",
        effects=None,
        time=1000,
        weather=None,
        dimension="overworld",
        complete=True,
        selected_slot=selected_slot,
    )


class AgFp30Body:
    def __init__(self, *, owner=None, pending_action_count=0, selected_slot=7):
        self.owner = owner
        self.pending_action_count = pending_action_count
        self.selected_slot = selected_slot
        self.items = {
            "dandelion": 1,
            "poppy": 1,
            "blue_orchid": 1,
            "porkchop": 1,
            "beef": 1,
            "mutton": 1,
            "torch": 16,
            "iron_ingot": 3,
            "shield": 1,
            "iron_pickaxe": 1,
        }

    def perceive(self, scope, params):
        if scope != "inventory":
            return PerceptionResult("Bot", scope, "perception", False, False, {}, error="unsupported")
        slots = []
        for index, (item, count) in enumerate(self.items.items()):
            slot = 40 if item == "shield" else 7 if item == "iron_pickaxe" else index if index < 7 else index + 1
            label = "offhand" if slot == 40 else "hotbar.7" if slot == 7 else f"inventory.{index}"
            slots.append(
                {
                    "slot": slot,
                    "item": f"minecraft:{item}",
                    "count": count,
                    "empty": False,
                    "slotLabel": label,
                }
            )
        start = int(params.get("start") or 0)
        limit = int(params.get("limit") or 12)
        page = slots[start : start + limit]
        next_start = start + limit if start + limit < len(slots) else None
        return PerceptionResult(
            "Bot",
            "inventory",
            "perception",
            True,
            next_start is None,
            {"slots": page, "nextStart": next_start},
        )

    def get_state(self):
        return _state(self.selected_slot)

    def event_head(self, _epoch):
        return {
            "owner": self.owner,
            "pending_action_count": self.pending_action_count,
        }


class BrokenAgFp30Body(AgFp30Body):
    def perceive(self, scope, params):
        raise RuntimeError("RCON socket closed")


class AgFp30TerminalTruthTests(unittest.TestCase):
    def test_canonical_goal_is_frozen_and_uses_composite_target(self):
        self.assertTrue(is_ag_fp30_goal(AG_FP30_GOAL))
        truth = evaluate_terminal_truth(
            AgFp30Body(),
            AG_FP30_GOAL,
            SessionStep("completed_turn", LifecycleState.ACTIVE),
        )

        self.assertTrue(truth.satisfied)
        self.assertEqual(truth.target.kind, "production_terminal")
        self.assertEqual(truth.target.goal_id, "AG-FP30")
        self.assertEqual(truth.to_trace()["target"]["goal_id"], "AG-FP30")
        self.assertTrue(truth.to_trace()["facts"]["terminal_satisfied"])

    def test_equipment_owner_and_pending_facts_are_independent_requirements(self):
        body = AgFp30Body(owner="moveTo", pending_action_count=1)
        truth = evaluate_ag_fp30_terminal_truth(
            body,
            AG_FP30_GOAL,
            SessionStep("completed_turn", LifecycleState.ACTIVE),
        )

        self.assertFalse(truth.satisfied)
        self.assertEqual(truth.facts["body_owner"], "moveTo")
        self.assertEqual(truth.facts["pending_action_count"], 1)
        self.assertTrue(truth.facts["equipment"]["offhand"]["satisfied"])
        self.assertTrue(truth.facts["equipment"]["mainhand"]["satisfied"])

    def test_missing_authoritative_inventory_is_not_model_success(self):
        truth = evaluate_ag_fp30_terminal_truth(
            BrokenAgFp30Body(),
            AG_FP30_GOAL,
            SessionStep("completed_turn", LifecycleState.ACTIVE),
        )

        self.assertFalse(truth.satisfied)
        self.assertFalse(truth.facts["inventory"]["ok"])
        self.assertIn("RCON socket closed", truth.facts["error"])

    def test_mainhand_requires_server_selected_slot(self):
        truth = evaluate_ag_fp30_terminal_truth(
            AgFp30Body(selected_slot=0),
            AG_FP30_GOAL,
            SessionStep("completed_turn", LifecycleState.ACTIVE),
        )

        self.assertFalse(truth.satisfied)
        self.assertFalse(truth.facts["equipment"]["mainhand"]["satisfied"])
        self.assertEqual(truth.facts["equipment"]["mainhand"]["selected_slot"], 0)


if __name__ == "__main__":
    unittest.main()
