import unittest

from minebot.body.interaction_support import NearbyBlockTarget, entity_stand_points, interaction_stand_points
from minebot.body.reach import (
    ReachIntent,
    block_reach_domain,
    block_reach_domains,
    round_robin_reach_goals,
)
from minebot.contract import ToolResult
from tests.unit.test_work_runtime import FakeBody


class NativeStandingBody(FakeBody):
    def __init__(self, blocks, heights):
        super().__init__(blocks=blocks)
        self.heights = heights

    def perceive(self, scope, params=None):
        result = super().perceive(scope, params)
        if scope == "blockCells" and params.get("include_standing") is True:
            for fact in result.data.get("cells", []):
                fact["standing_y"] = self.heights.get((fact["x"], fact["y"], fact["z"]))
        return result


class ReachRuntimeTests(unittest.TestCase):
    def test_expanded_vertical_band_does_not_discard_a_reachable_fractional_pose(self):
        stand = (0, 62, 0)
        body = NativeStandingBody({stand: ("minecraft:snow", "SOLID")}, {stand: 62.875})
        stands = interaction_stand_points(body, (1, 64, 0),
            expand_vertical=True, interaction_radius=1.95)
        self.assertIn(stand, stands)

    def test_expanded_vertical_band_still_requires_actual_range_truth(self):
        stand = (0, 62, 0)
        body = NativeStandingBody({stand: ("minecraft:snow", "SOLID")}, {stand: 62.0})
        stands = interaction_stand_points(body, (1, 64, 0),
            expand_vertical=True, interaction_radius=1.95)
        self.assertNotIn(stand, stands)
        requested = [tuple(cell) for scope, params in body.perceptions if scope == "blockCells"
            for cell in params["cells"]]
        self.assertIn(stand, requested)

    def test_vertical_expansion_preserves_explicit_same_column_candidates(self):
        stand = (1, 65, 0)
        body = NativeStandingBody({(1, 64, 0): ("minecraft:stone", "SOLID")}, {stand: 65.0})
        stands = interaction_stand_points(body, (1, 64, 0), include_target=True,
            expand_vertical=True, interaction_radius=0.9)
        self.assertIn(stand, stands)

    def test_entity_band_includes_a_native_low_surface_at_its_actual_distance(self):
        stand = (0, 63, 0)
        body = NativeStandingBody({stand: ("minecraft:stone_slab", "SOLID")}, {stand: 63.5})
        stands = entity_stand_points(body, (1.5, 63.5, 0.5), max_distance=1.05)
        self.assertIn(stand, stands)

    def test_entity_band_rejects_missing_native_standing_proof(self):
        body = NativeStandingBody({(0, 63, 0): ("minecraft:stone", "SOLID")}, {})
        stands = entity_stand_points(body, (1.5, 64, 0.5), max_distance=1.5)
        self.assertNotIn((0, 64, 0), stands)

    def test_native_low_surface_is_retained_using_its_physical_reach(self):
        stand = (0, 63, 0)
        body = NativeStandingBody({stand: ("minecraft:stone_slab", "SOLID")}, {stand: 63.5})
        domain = block_reach_domain(body, ReachIntent(target=(1, 64, 0),
            vertical_offsets=(-1,), interaction_radius=1.5))
        self.assertNotIsInstance(domain, ToolResult)
        self.assertIn(stand, domain.candidates)
        self.assertTrue(all(params["include_standing"] for scope, params in body.perceptions
            if scope == "blockCells"))

    def test_native_absence_of_support_cannot_fall_back_to_coarse_solid_floor(self):
        stand = (0, 64, 0)
        body = NativeStandingBody({(0, 63, 0): ("minecraft:stone", "SOLID")}, {})
        domain = block_reach_domain(body, ReachIntent(target=(1, 64, 0), vertical_offsets=(0,)))
        self.assertNotIn(stand, domain.candidates)

    def test_native_height_outside_interaction_range_is_not_a_valid_stand(self):
        stand = (0, 65, 0)
        body = NativeStandingBody({(0, 64, 0): ("minecraft:oak_fence", "SOLID")}, {stand: 65.5})
        domain = block_reach_domain(body, ReachIntent(target=(1, 64, 0),
            vertical_offsets=(1,), interaction_radius=1.3))
        self.assertNotIn(stand, domain.candidates)

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
