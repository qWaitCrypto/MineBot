import unittest

from minebot.body import BlockApproachTransactions, GetToBlockConfig
from minebot.contract import ToolResult
from tests.unit.test_work_runtime import FakeBody, FakeNavigator


def target_blocks(*targets: tuple[int, int, int]) -> dict[tuple[int, int, int], tuple[str, str]]:
    blocks: dict[tuple[int, int, int], tuple[str, str]] = {}
    for target in targets:
        blocks[target] = ("minecraft:oak_log", "SOLID")
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            stand = (target[0] + dx, target[1], target[2] + dz)
            blocks[stand] = ("minecraft:air", "CLEAR")
            blocks[(stand[0], stand[1] + 1, stand[2])] = ("minecraft:air", "CLEAR")
            blocks[(stand[0], stand[1] - 1, stand[2])] = ("minecraft:stone", "SOLID")
    return blocks


class SequencedNavigator(FakeNavigator):
    def __init__(self, body: FakeBody, outcomes: list[tuple[bool, str]]) -> None:
        super().__init__()
        self.body = body
        self.outcomes = list(outcomes)

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        selected = goal.goals[0].representative((0, 0, 0))
        success, reason = self.outcomes.pop(0)
        if success:
            self.body.state_pos = (float(selected[0]), float(selected[1]), float(selected[2]))
        return ToolResult(
            success=success,
            reason=reason,
            can_retry=not success,
            metrics={"goal": list(selected), "selected_goal": list(selected)},
        )


class ChangingTargetNavigator(SequencedNavigator):
    def __init__(self, body: FakeBody, target: tuple[int, int, int]) -> None:
        super().__init__(body, [(True, "arrived")])
        self.target = target

    def navigate_to(self, goal, **kwargs):
        result = super().navigate_to(goal, **kwargs)
        self.body.blocks[self.target] = ("minecraft:air", "CLEAR")
        return result


class BlockApproachTransactionsTests(unittest.TestCase):
    def test_requires_block_filter(self):
        body = FakeBody()
        navigator = FakeNavigator()

        result = BlockApproachTransactions(body, navigator).get_to_block(block_types=())

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "get_to_block_filter_missing")

    def test_already_in_range_rechecks_identity_without_navigation(self):
        target = (2, 64, 0)
        body = FakeBody(
            blocks=target_blocks(target),
            find_blocks=[{"x": target[0], "y": target[1], "z": target[2], "type": "minecraft:oak_log"}],
        )
        navigator = FakeNavigator()

        result = BlockApproachTransactions(body, navigator).get_to_block(block_types=("oak_log",))

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_reached")
        self.assertTrue(result.metrics["identity_verified"])
        self.assertTrue(result.metrics["range_verified"])
        self.assertTrue(result.metrics["already_in_range"])
        self.assertEqual(navigator.calls, [])

    def test_distant_target_uses_complete_pure_movement_stand_domain(self):
        target = (8, 64, 0)
        body = FakeBody(
            blocks=target_blocks(target),
            find_blocks=[{"x": target[0], "y": target[1], "z": target[2], "type": "minecraft:oak_log"}],
        )
        navigator = FakeNavigator()
        navigator.body = body

        result = BlockApproachTransactions(body, navigator).get_to_block(block_types=("oak_log",))

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_reached")
        self.assertEqual(result.metrics["target"], list(target))
        self.assertEqual(len(navigator.calls), 1)
        goal, kwargs = navigator.calls[0]
        self.assertEqual(len(goal.goals), 4)
        config = kwargs["config"]
        self.assertFalse(config.allow_break)
        self.assertEqual(config.max_break_steps, 0)
        self.assertFalse(config.allow_place)
        self.assertFalse(config.allow_pillar)
        self.assertFalse(config.allow_downward)

    def test_distant_high_target_uses_a_lower_stand_within_interaction_reach(self):
        target = (8, 75, 0)
        lower_stand = (7, 73, 0)
        blocks = {target: ("minecraft:oak_log", "SOLID")}
        for stand_y in (target[1], target[1] - 1):
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                blocks[(target[0] + dx, stand_y, target[2] + dz)] = (
                    "minecraft:oak_leaves",
                    "SOLID",
                )
        blocks[lower_stand] = ("minecraft:air", "CLEAR")
        blocks[(lower_stand[0], lower_stand[1] + 1, lower_stand[2])] = ("minecraft:air", "CLEAR")
        blocks[(lower_stand[0], lower_stand[1] - 1, lower_stand[2])] = ("minecraft:stone", "SOLID")
        body = FakeBody(
            blocks=blocks,
            find_blocks=[{"x": target[0], "y": target[1], "z": target[2], "type": "minecraft:oak_log"}],
        )
        navigator = FakeNavigator()
        navigator.body = body

        result = BlockApproachTransactions(body, navigator).get_to_block(
            block_types=("oak_log",),
            config=GetToBlockConfig(candidate_budget=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "block_reached")
        self.assertEqual(result.metrics["selected_goal"], list(lower_stand))
        domain = result.metrics["attempts"][0]["domain"]
        self.assertTrue(domain["candidate_targets"][0]["expanded_vertical"])
        self.assertIn(lower_stand, tuple(goal.pos for goal in navigator.calls[0][0].goals))
        self.assertGreaterEqual(
            [scope for scope, _params in body.perceptions].count("blockCells"),
            3,
        )

    def test_navigation_failure_blacklists_selected_candidate_and_replans(self):
        near = (8, 64, 0)
        far = (12, 64, 0)
        body = FakeBody(
            blocks=target_blocks(near, far),
            find_blocks=[
                {"x": near[0], "y": near[1], "z": near[2], "type": "minecraft:oak_log"},
                {"x": far[0], "y": far[1], "z": far[2], "type": "minecraft:oak_log"},
            ],
        )
        navigator = SequencedNavigator(body, [(False, "no_path"), (True, "arrived")])
        runtime = BlockApproachTransactions(body, navigator)

        result = runtime.get_to_block(
            block_types=("oak_log",),
            config=GetToBlockConfig(candidate_budget=2, candidate_batch_size=1),
        )

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["target"], list(far))
        self.assertEqual(result.metrics["candidate_blacklist"], [list(near)])
        self.assertEqual(len(navigator.calls), 2)
        self.assertEqual(result.metrics["attempts"][0]["navigation"]["reason"], "no_path")

    def test_post_move_identity_change_is_not_reported_as_reached(self):
        target = (8, 64, 0)
        body = FakeBody(
            blocks=target_blocks(target),
            find_blocks=[{"x": target[0], "y": target[1], "z": target[2], "type": "minecraft:oak_log"}],
        )
        navigator = ChangingTargetNavigator(body, target)

        result = BlockApproachTransactions(body, navigator).get_to_block(
            block_types=("oak_log",),
            config=GetToBlockConfig(candidate_budget=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "get_to_block_candidate_budget_exhausted")
        verification = result.metrics["attempts"][0]["verification"]
        self.assertEqual(verification["reason"], "get_to_block_target_unusable")
        self.assertEqual(
            verification["metrics"]["failures"][0]["reason"],
            "get_to_block_target_changed",
        )


if __name__ == "__main__":
    unittest.main()
