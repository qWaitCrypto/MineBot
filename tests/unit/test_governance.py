import unittest

from minebot.contract import StructureRiskAssessment, StructureRiskLevel
from minebot.game.governance import BreakContext, GovernancePolicy, InteractionContext, PlaceContext, Region


class StaticRiskAssessor:
    def __init__(self, level, *, complete=True):
        self.level = level
        self.complete = complete
        self.calls = []

    def assess(self, pos, block_type, context, *, bot_placements=None):
        self.calls.append((pos, block_type, context))
        return StructureRiskAssessment(
            pos=pos,
            block_type=block_type,
            level=self.level,
            score=0.9 if self.level is StructureRiskLevel.HIGH else 0.2,
            complete=self.complete,
            sampled_cells=63,
            signals=("test_signal",),
        )


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.policy = GovernancePolicy(
            natural_regions=[Region("test_mine", (-10, 0, -10), (10, 100, 10))],
            protected_regions=[Region("base", (20, 0, 20), (30, 100, 30))],
        )

    def test_natural_type_breakable_outside_declared_region(self):
        # Provenance is by block TYPE for resource/explicit breaks: a known-natural
        # block is breakable even with no natural_region containing it.
        decision = self.policy.can_break((100, 64, 100), "minecraft:stone", BreakContext.DIRECT)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural")
        self.assertIsNone(decision.natural_region)

    def test_travel_break_stays_conservative_outside_region(self):
        # Pathing does NOT tunnel through undeclared terrain even for a natural
        # type: TRAVEL remains region-gated so navigation behavior is unchanged.
        decision = self.policy.can_break((100, 64, 100), "stone", BreakContext.TRAVEL)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "unknown_provenance")

    def test_collect_dirt_allowed_outside_declared_region(self):
        # The executability unblock: collect a COLLECT_TARGET with no declared
        # region (the real-server case) must be allowed.
        decision = self.policy.can_break((100, 64, 100), "dirt", BreakContext.COLLECT)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural")
        self.assertIsNone(decision.natural_region)

    def test_unknown_type_still_denied_outside_region(self):
        # A type we do not recognize as natural stays protected (red-line safe
        # default for unknown/player blocks), now by type rather than region.
        decision = self.policy.can_break((100, 64, 100), "cobblestone_wall", BreakContext.COLLECT)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "not_natural_breakable")

    def test_protected_region_wins_over_natural_classification(self):
        decision = self.policy.can_break((25, 64, 25), "stone", BreakContext.COLLECT)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_region")
        self.assertEqual(decision.details["region"], "base")

    def test_strongly_protected_types_are_not_breakable_inside_natural_region(self):
        decision = self.policy.can_break((0, 64, 0), "chest", BreakContext.DIRECT)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_type")

    def test_path_context_does_not_strip_mine_collectable_ore(self):
        decision = self.policy.can_break((0, 64, 0), "diamond_ore", BreakContext.PATH)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "path_no_terrain_break")

    def test_collect_context_allows_collect_target_inside_natural_region(self):
        decision = self.policy.can_break((0, 64, 0), "diamond_ore", BreakContext.COLLECT)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural")
        self.assertEqual(decision.natural_region, "test_mine")

    def test_collect_context_allows_phase1_surface_resources_inside_natural_region(self):
        for block_type in ("dirt", "sand", "gravel"):
            with self.subTest(block_type=block_type):
                decision = self.policy.can_break((0, 64, 0), block_type, BreakContext.COLLECT)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason, "allowed_natural")

    def test_collect_context_allows_safe_natural_flower_types(self):
        for block_type in ("dandelion", "poppy", "blue_orchid", "cornflower"):
            with self.subTest(block_type=block_type):
                decision = self.policy.can_break((0, 64, 0), block_type, BreakContext.COLLECT)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason, "allowed_natural")

    def test_explicit_natural_leaf_egress_remains_subject_to_structure_and_region_protection(self):
        low_risk = GovernancePolicy(
            structure_risk_assessor=StaticRiskAssessor(StructureRiskLevel.LOW),
            require_structure_assessment=True,
        )
        high_risk = GovernancePolicy(
            structure_risk_assessor=StaticRiskAssessor(StructureRiskLevel.HIGH),
            require_structure_assessment=True,
        )
        protected = GovernancePolicy(
            protected_regions=[Region("leaf_build", (99, 60, 99), (101, 70, 101))],
            structure_risk_assessor=StaticRiskAssessor(StructureRiskLevel.LOW),
            require_structure_assessment=True,
        )

        allowed = low_risk.can_break(
            (100, 64, 100),
            "minecraft:spruce_leaves",
            BreakContext.DIRECT,
            explicit_target=True,
        )
        denied_high = high_risk.can_break(
            (100, 64, 100),
            "spruce_leaves",
            BreakContext.DIRECT,
            explicit_target=True,
        )
        denied_protected = protected.can_break(
            (100, 64, 100),
            "spruce_leaves",
            BreakContext.DIRECT,
            explicit_target=True,
        )

        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.reason, "allowed_natural")
        self.assertFalse(denied_high.allowed)
        self.assertEqual(denied_high.reason, "player_structure_risk")
        self.assertFalse(denied_protected.allowed)
        self.assertEqual(denied_protected.reason, "protected_region")

    def test_collect_context_rejects_non_target_trash(self):
        decision = self.policy.can_break((0, 64, 0), "stone", BreakContext.COLLECT)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "collect_target_required")

    def test_collect_context_allows_explicit_non_default_target(self):
        decision = self.policy.can_break(
            (0, 64, 0),
            "stone",
            BreakContext.COLLECT,
            explicit_target=True,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural")

    def test_collect_approach_allows_encasing_stone_outside_declared_region(self):
        # The dig-through root cause fix: to reach a BURIED collect target the
        # navigator must clear the natural blocks ON THE WAY (e.g. encasing
        # stone). Plain COLLECT refuses stone (collect_target_required); COLLECT
        # _APPROACH permits any natural block as pathing — like TRAVEL's
        # allowed_natural, but TYPE-gated so it does not require a declared
        # region (TRAVEL refuses undeclared terrain). This is the only context
        # that lets collect dig over to a buried target in the real
        # default-spawn (undeclared) world.
        decision = self.policy.can_break((100, 64, 100), "stone", BreakContext.COLLECT_APPROACH)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural")
        self.assertIsNone(decision.natural_region)

    def test_collect_approach_allows_target_type_too(self):
        decision = self.policy.can_break((100, 64, 100), "diamond_ore", BreakContext.COLLECT_APPROACH)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural")

    def test_collect_approach_still_refuses_player_functional_blocks(self):
        # Red line unchanged: player functional blocks stay protected even under
        # the dig-through pathing context.
        for block_type in ("chest", "oak_door", "crafting_table"):
            with self.subTest(block_type=block_type):
                decision = self.policy.can_break((100, 64, 100), block_type, BreakContext.COLLECT_APPROACH)
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.protected)
                self.assertEqual(decision.reason, "protected_type")

    def test_collect_approach_still_refuses_protected_region(self):
        decision = self.policy.can_break((25, 64, 25), "stone", BreakContext.COLLECT_APPROACH)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_region")

    def test_collect_approach_still_refuses_non_natural_types(self):
        decision = self.policy.can_break((100, 64, 100), "cobblestone_wall", BreakContext.COLLECT_APPROACH)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "not_natural_breakable")

    def test_recovery_context_routes_around_deliberate_resource_targets(self):
        for block_type in ("iron_ore", "oak_log"):
            with self.subTest(block_type=block_type):
                decision = self.policy.can_break((0, 64, 0), block_type, BreakContext.RECOVERY)

                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "recovery_no_resource_break")

    def test_recovery_context_can_clear_ordinary_surface_cover(self):
        for block_type in ("dirt", "grass_block", "gravel", "sand"):
            with self.subTest(block_type=block_type):
                decision = self.policy.can_break((0, 64, 0), block_type, BreakContext.RECOVERY)

                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason, "allowed_natural")

    def test_farm_context_allows_supported_crop_inside_natural_region(self):
        decision = self.policy.can_break((0, 64, 0), "wheat", BreakContext.FARM)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural_farm")
        self.assertEqual(decision.natural_region, "test_mine")

    def test_farm_context_rejects_supported_crop_with_unknown_provenance(self):
        decision = self.policy.can_break((100, 64, 100), "wheat", BreakContext.FARM)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "unknown_provenance")

    def test_bot_cleanup_allows_matching_bot_owned_temporary_block(self):
        self.policy.record_bot_placement((1, 64, 1), "cobblestone", "scaffold", "Bot1")

        decision = self.policy.can_break((1, 64, 1), "minecraft:cobblestone", BreakContext.BOT_CLEANUP)

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.bot_owned)
        self.assertEqual(decision.reason, "allowed_bot_owned")

    def test_bot_ledger_type_mismatch_blocks_cleanup(self):
        self.policy.record_bot_placement((1, 64, 1), "cobblestone", "scaffold", "Bot1")

        decision = self.policy.can_break((1, 64, 1), "stone", BreakContext.BOT_CLEANUP)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "bot_ledger_type_mismatch")

    def test_place_requires_known_region_unless_direct(self):
        decision = self.policy.can_place((100, 64, 100), "cobblestone", PlaceContext.TRAVEL, "Bot1")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "unknown_provenance")

    def test_place_allowed_in_natural_region(self):
        decision = self.policy.can_place((0, 64, 0), "cobblestone", PlaceContext.TRAVEL, "Bot1")

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_place")
        self.assertEqual(decision.natural_region, "test_mine")

    def test_work_context_allows_bot_owned_workstation_in_natural_region(self):
        decision = self.policy.can_place((0, 64, 0), "crafting_table", PlaceContext.WORK, "Bot1")

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_place")

    def test_travel_context_does_not_use_workstation_as_scaffold(self):
        decision = self.policy.can_place((0, 64, 0), "crafting_table", PlaceContext.TRAVEL, "Bot1")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "protected_type")

    def test_place_blocked_in_protected_region(self):
        decision = self.policy.can_place((25, 64, 25), "cobblestone", PlaceContext.DIRECT, "Bot1")

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_region")

    def test_interaction_allowed_inside_natural_region(self):
        decision = self.policy.can_interact((0, 64, 0), "oak_door", InteractionContext.ACTIVATE)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_interaction")
        self.assertEqual(decision.natural_region, "test_mine")
        self.assertEqual(decision.details["context"], "activate")

    def test_interaction_blocked_in_protected_region(self):
        decision = self.policy.can_interact((25, 64, 25), "oak_door", InteractionContext.ACTIVATE)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "protected_region")
        self.assertEqual(decision.details["context"], "activate")

    def test_interaction_blocks_unknown_provenance(self):
        decision = self.policy.can_interact((100, 64, 100), "farmland", InteractionContext.FARM)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "unknown_provenance")
        self.assertEqual(decision.details["context"], "farm")

    def test_required_structure_assessment_never_falls_back_to_block_type(self):
        policy = GovernancePolicy(require_structure_assessment=True)

        decision = policy.can_break((100, 64, 100), "stone", BreakContext.DIRECT)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "structure_assessment_required")

    def test_high_structure_risk_denies_raw_natural_block(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.HIGH)
        policy = GovernancePolicy(
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )

        decision = policy.can_break((100, 64, 100), "stone", BreakContext.DIRECT)

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.protected)
        self.assertEqual(decision.reason, "player_structure_risk")
        self.assertEqual(decision.details["structure_risk"]["level"], "high")

    def test_ambiguous_structure_risk_requires_more_evidence_or_new_candidate(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.AMBIGUOUS)
        policy = GovernancePolicy(
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )

        decision = policy.can_break((100, 64, 100), "dirt", BreakContext.COLLECT)

        self.assertFalse(decision.allowed)
        self.assertFalse(decision.protected)
        self.assertEqual(decision.reason, "governance_confirmation_required")
        confirmation = decision.details["governance_confirmation"]
        self.assertEqual(confirmation["context"], "collect")
        self.assertEqual(confirmation["block_types"], ["dirt"])
        self.assertEqual(confirmation["max_unique_blocks"], 12)

    def test_confirmed_scope_allows_multiple_matching_blocks_after_fresh_assessment(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.AMBIGUOUS)
        policy = GovernancePolicy(
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
            bot_name="Bot1",
        )

        first = policy.can_break(
            (100, 64, 100),
            "stone",
            BreakContext.DIRECT,
            bot="Bot1",
            action_id="action-1",
        )
        challenge_id = first.details["governance_confirmation"]["challenge_id"]
        confirmed = policy.confirm_scope(challenge_id, "mine natural stone for the current task")
        same_target = policy.can_break((100, 64, 100), "stone", BreakContext.DIRECT, bot="Bot1")
        nearby_target = policy.can_break((107, 67, 100), "stone", BreakContext.DIRECT, bot="Bot1")

        self.assertTrue(confirmed["confirmed"])
        self.assertTrue(same_target.allowed)
        self.assertTrue(nearby_target.allowed)
        self.assertEqual(same_target.reason, "allowed_governance_scope")
        self.assertEqual(nearby_target.reason, "allowed_governance_scope")
        self.assertEqual(len(assessor.calls), 3)
        permit = nearby_target.details["governance_permit"]
        self.assertEqual(permit["authorized_unique_blocks"], 2)

    def test_scope_confirmation_does_not_cover_other_type_context_or_region(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.AMBIGUOUS)
        policy = GovernancePolicy(
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )
        first = policy.can_break((0, 64, 0), "stone", BreakContext.DIRECT)
        policy.confirm_scope(
            first.details["governance_confirmation"]["challenge_id"],
            "clear natural stone",
        )

        other_type = policy.can_break((1, 64, 0), "deepslate", BreakContext.DIRECT)
        other_context = policy.can_break((1, 64, 0), "stone", BreakContext.RECOVERY)
        outside_region = policy.can_break((9, 64, 0), "stone", BreakContext.DIRECT)

        for decision in (other_type, other_context, outside_region):
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "governance_confirmation_required")

    def test_recovery_confirmation_covers_one_bounded_vertical_escape_corridor(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.AMBIGUOUS)
        policy = GovernancePolicy(
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )

        decision = policy.can_break((0, 64, 0), "stone", BreakContext.RECOVERY)
        challenge = decision.details["governance_confirmation"]

        self.assertEqual(challenge["region"]["min"], [-16, 32, -16])
        self.assertEqual(challenge["region"]["max"], [16, 96, 16])
        self.assertEqual(challenge["max_unique_blocks"], 96)

    def test_confirmation_never_overrides_high_risk_incomplete_reads_or_protected_regions(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.AMBIGUOUS)
        policy = GovernancePolicy(
            protected_regions=[Region("base", (1, 0, -1), (2, 100, 1))],
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )
        first = policy.can_break((0, 64, 0), "stone", BreakContext.DIRECT)
        policy.confirm_scope(
            first.details["governance_confirmation"]["challenge_id"],
            "clear natural stone",
        )

        protected = policy.can_break((1, 64, 0), "stone", BreakContext.DIRECT)
        assessor.level = StructureRiskLevel.HIGH
        high = policy.can_break((0, 64, 0), "stone", BreakContext.DIRECT)
        assessor.level = StructureRiskLevel.AMBIGUOUS
        assessor.complete = False
        incomplete = policy.can_break((0, 64, 0), "stone", BreakContext.DIRECT)

        self.assertEqual((protected.allowed, protected.reason), (False, "protected_region"))
        self.assertEqual((high.allowed, high.reason), (False, "player_structure_risk"))
        self.assertEqual((incomplete.allowed, incomplete.reason), (False, "structure_risk_unknown"))

    def test_confirmation_expires_and_is_invalidated_by_task_scope(self):
        now = [10.0]
        task_scope = ["task-1"]
        assessor = StaticRiskAssessor(StructureRiskLevel.AMBIGUOUS)
        policy = GovernancePolicy(
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
            confirmation_ttl_s=5.0,
            clock=lambda: now[0],
        )
        policy.begin_confirmation_epoch(lambda: task_scope[0])
        first = policy.can_break((0, 64, 0), "stone", BreakContext.DIRECT)
        policy.confirm_scope(
            first.details["governance_confirmation"]["challenge_id"],
            "clear natural stone",
        )
        self.assertTrue(policy.can_break((0, 64, 0), "stone", BreakContext.DIRECT).allowed)

        task_scope[0] = "task-2"
        scope_invalid = policy.can_break((0, 64, 0), "stone", BreakContext.DIRECT)
        self.assertEqual(scope_invalid.reason, "governance_confirmation_required")
        policy.confirm_scope(
            scope_invalid.details["governance_confirmation"]["challenge_id"],
            "clear natural stone",
        )
        now[0] += 6.0
        expired = policy.can_break((0, 64, 0), "stone", BreakContext.DIRECT)
        self.assertEqual(expired.reason, "governance_confirmation_required")

    def test_low_structure_risk_allows_context_legal_natural_target(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.LOW)
        policy = GovernancePolicy(
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )

        decision = policy.can_break((100, 64, 100), "diamond_ore", BreakContext.COLLECT)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_natural")
        self.assertEqual(decision.details["structure_risk"]["source"], "voxel")

    def test_bot_cleanup_uses_exact_ledger_without_probabilistic_assessment(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.HIGH)
        policy = GovernancePolicy(
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )
        policy.record_bot_placement((1, 64, 1), "cobblestone", "bridge", "Bot1")

        decision = policy.can_break((1, 64, 1), "cobblestone", BreakContext.BOT_CLEANUP)

        self.assertTrue(decision.allowed)
        self.assertEqual(assessor.calls, [])

    def test_ambiguous_mutation_risk_does_not_deny_non_mutating_stand(self):
        assessor = StaticRiskAssessor(StructureRiskLevel.AMBIGUOUS)
        policy = GovernancePolicy(
            natural_regions=[Region("surface", (-10, 0, -10), (10, 100, 10))],
            structure_risk_assessor=assessor,
            require_structure_assessment=True,
        )

        break_decision = policy.can_break((0, 63, 0), "stone", BreakContext.DIRECT)
        stand_decision = policy.can_stand((0, 63, 0), "stone")

        self.assertFalse(break_decision.allowed)
        self.assertEqual(break_decision.reason, "governance_confirmation_required")
        self.assertTrue(stand_decision.allowed)
        self.assertEqual(stand_decision.reason, "allowed_stand")
        self.assertEqual(stand_decision.natural_region, "surface")
        self.assertEqual(len(assessor.calls), 1)

    def test_stand_still_refuses_protected_regions_and_support_types(self):
        protected = self.policy.can_stand((25, 64, 25), "stone")
        functional = self.policy.can_stand((0, 64, 0), "crafting_table")
        hazardous = self.policy.can_stand((0, 64, 0), "magma_block")

        self.assertEqual((protected.allowed, protected.reason), (False, "protected_region"))
        self.assertEqual((functional.allowed, functional.reason), (False, "protected_support_type"))
        self.assertEqual((hazardous.allowed, hazardous.reason), (False, "unsafe_support_type"))

    def test_stand_allows_matching_bot_owned_support_without_natural_region(self):
        policy = GovernancePolicy()
        policy.record_bot_placement((100, 64, 100), "cobblestone", "bridge", "Bot1")

        decision = policy.can_stand((100, 64, 100), "minecraft:cobblestone")

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.bot_owned)
        self.assertEqual(decision.reason, "allowed_bot_owned")


if __name__ == "__main__":
    unittest.main()
