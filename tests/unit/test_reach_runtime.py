import unittest

from minebot.body.interaction_support import NearbyBlockTarget
from minebot.body.reach import (
    ReachIntent,
    block_reach_domain,
    block_reach_domains,
    round_robin_reach_goals,
)
from minebot.contract import ToolResult
from tests.unit.test_work_runtime import FakeBody


class ReachRuntimeTests(unittest.TestCase):
    def test_round_robin_goal_domain_preserves_target_links_and_bound(self):
        targets = (
            NearbyBlockTarget((5, 64, 0), "dirt", 5.0),
            NearbyBlockTarget((8, 64, 0), "stone", 8.0),
        )
        goals, linked = round_robin_reach_goals(
            targets,
            {
                targets[0].pos: ((5, 64, -1), (5, 64, 1)),
                targets[1].pos: ((8, 64, -1), (8, 64, 1)),
            },
            max_goals=3,
            target_position=lambda target: target.pos,
        )

        self.assertEqual(goals, ((5, 64, -1), (8, 64, -1), (5, 64, 1)))
        self.assertEqual(linked[goals[0]], (targets[0],))
        self.assertEqual(linked[goals[1]], (targets[1],))
        self.assertEqual(linked[goals[2]], (targets[0],))

    def test_vertical_reach_uses_one_generic_domain(self):
        target = (5, 67, 0)
        stand = (5, 65, -1)
        body = FakeBody(
            blocks={
                target: ("minecraft:oak_log", "SOLID"),
                stand: ("minecraft:air", "CLEAR"),
                (stand[0], stand[1] + 1, stand[2]): ("minecraft:air", "CLEAR"),
                (stand[0], stand[1] - 1, stand[2]): ("minecraft:stone", "SOLID"),
            }
        )

        domain = block_reach_domain(
            body,
            ReachIntent(
                target=target,
                vertical_offsets=None,
                movement_profile="resource_collection",
                mutation_profile="governed_break",
            ),
        )

        self.assertNotIsInstance(domain, ToolResult)
        self.assertIn(stand, domain.candidates)
        self.assertEqual(domain.diagnostics["terminal_predicate"], "within_interaction_range")
        self.assertGreater(domain.diagnostics["geometric_candidate_count"], 4)

    def test_multiple_targets_share_one_authoritative_cell_batch(self):
        targets = ((5, 64, 0), (8, 64, 0))
        blocks = {}
        for target in targets:
            stand = (target[0], target[1], target[2] - 1)
            blocks.update(
                {
                    target: ("minecraft:dirt", "SOLID"),
                    stand: ("minecraft:air", "CLEAR"),
                    (stand[0], stand[1] + 1, stand[2]): ("minecraft:air", "CLEAR"),
                    (stand[0], stand[1] - 1, stand[2]): ("minecraft:stone", "SOLID"),
                }
            )
        body = FakeBody(blocks=blocks)

        domains = block_reach_domains(
            body,
            tuple(ReachIntent(target=target, vertical_offsets=(0,)) for target in targets),
        )

        self.assertEqual(len(domains), 2)
        self.assertTrue(all(domain.candidates for domain in domains))
        self.assertEqual(
            [scope for scope, _params in body.perceptions].count("blockCells"),
            1,
        )

    def test_line_of_sight_rejects_occluded_vertical_stand(self):
        target = (5, 67, 0)
        stand = (5, 65, -1)
        body = FakeBody(
            blocks={
                target: ("minecraft:oak_log", "SOLID"),
                stand: ("minecraft:air", "CLEAR"),
                (stand[0], stand[1] + 1, stand[2]): ("minecraft:air", "CLEAR"),
                (stand[0], stand[1] - 1, stand[2]): ("minecraft:stone", "SOLID"),
                (5, 66, 0): ("minecraft:stone", "SOLID"),
            }
        )

        domain = block_reach_domain(
            body,
            ReachIntent(
                target=target,
                vertical_offsets=(-2,),
                interaction_radius=4.5,
                require_line_of_sight=True,
            ),
        )

        self.assertNotIsInstance(domain, ToolResult)
        self.assertNotIn(stand, domain.candidates)
        self.assertTrue(
            any(
                rejection["candidate"] == list(stand)
                and rejection["reason"] == "target_occluded"
                and rejection["cell"] == [5, 66, 0]
                for rejection in domain.rejected
            )
        )

    def test_governed_clearance_returns_solid_obstruction_with_evidence(self):
        target = (5, 64, 0)
        stand = (5, 64, -1)
        body = FakeBody(
            blocks={
                target: ("minecraft:iron_ore", "SOLID"),
                stand: ("minecraft:dirt", "SOLID"),
                (stand[0], stand[1] + 1, stand[2]): ("minecraft:dirt", "SOLID"),
                (stand[0], stand[1] - 1, stand[2]): ("minecraft:stone", "SOLID"),
            }
        )

        domain = block_reach_domain(
            body,
            ReachIntent(
                target=target,
                vertical_offsets=(0,),
                mutation_profile="governed_break",
                clearance_profile="governed",
                require_line_of_sight=True,
            ),
        )

        self.assertNotIsInstance(domain, ToolResult)
        self.assertIn(stand, domain.candidates)
        self.assertTrue(any(entry["candidate"] == list(stand) for entry in domain.clearance))
        self.assertTrue(
            any(
                requirement["reason"] == "feet_blocked"
                for entry in domain.clearance
                if entry["candidate"] == list(stand)
                for requirement in entry["requirements"]
            )
        )

    def test_governed_clearance_does_not_promote_unknown_obstruction(self):
        target = (5, 64, 0)
        stand = (5, 64, -1)
        body = FakeBody(
            blocks={
                target: ("minecraft:iron_ore", "SOLID"),
                stand: ("minecraft:unknown", "UNKNOWN"),
                (stand[0], stand[1] + 1, stand[2]): ("minecraft:air", "CLEAR"),
                (stand[0], stand[1] - 1, stand[2]): ("minecraft:stone", "SOLID"),
            }
        )

        domain = block_reach_domain(
            body,
            ReachIntent(
                target=target,
                vertical_offsets=(0,),
                mutation_profile="governed_break",
                clearance_profile="governed",
                require_line_of_sight=True,
            ),
        )

        self.assertNotIsInstance(domain, ToolResult)
        self.assertNotIn(stand, domain.candidates)
        self.assertTrue(any(rejection["reason"] == "feet_blocked" for rejection in domain.rejected))

    def test_governed_clearance_keeps_target_occlusion_as_hard_rejection(self):
        target = (5, 67, 0)
        stand = (5, 65, -1)
        body = FakeBody(
            blocks={
                target: ("minecraft:iron_ore", "SOLID"),
                stand: ("minecraft:air", "CLEAR"),
                (stand[0], stand[1] + 1, stand[2]): ("minecraft:air", "CLEAR"),
                (stand[0], stand[1] - 1, stand[2]): ("minecraft:stone", "SOLID"),
                (5, 66, 0): ("minecraft:stone", "SOLID"),
            }
        )

        domain = block_reach_domain(
            body,
            ReachIntent(
                target=target,
                vertical_offsets=(-2,),
                clearance_profile="governed",
                require_line_of_sight=True,
            ),
        )

        self.assertNotIsInstance(domain, ToolResult)
        self.assertNotIn(stand, domain.candidates)
        self.assertTrue(any(rejection["reason"] == "target_occluded" for rejection in domain.rejected))


if __name__ == "__main__":
    unittest.main()
