import unittest

from minebot.brain.progress import FAILURE_STORM_LIMIT, STAGNATION_LIMIT, STALL_LIMIT, ProgressAbort, ProgressAuthority
from minebot.contract import BodyState
from minebot.contract.progress import LocalProgressController
from minebot.brain.progress import combat_progress_evidence


class CombatProgressEvidenceTests(unittest.TestCase):
    def receipt(self, *, success=True, reason="killed", **metrics):
        return {"success": success, "reason": reason, "metrics": {
            "event": "engageDone", "action_id": "action-1",
            "target_uuid": "target-1", "target_id": "target-1",
            "persistent_target": True, "damage_observed": True,
            "target_initial_health": 20.0, "target_health": 0.0,
            **metrics,
        }}

    def test_observed_damage_can_be_partial_output_without_a_kill(self):
        for reason in ("timeout", "disengaged_low_health", "no_path"):
            with self.subTest(reason=reason):
                self.assertEqual(combat_progress_evidence(self.receipt(
                    success=False, reason=reason, target_health=5.0,
                )), ("observed_target_damage",))

    def test_kill_label_and_swing_count_alone_are_not_effects(self):
        invalid = [
            {"target_initial_health": None}, {"target_health": None},
            {"target_health": 10.0}, {"target_health": -1.0},
            {"target_health": float("nan")}, {"target_initial_health": float("inf")},
            {"target_initial_health": True}, {"target_health": "0"},
            {"target_initial_health": 0.0}, {"damage_observed": False, "attacks": 20},
            {"target_id": "other-target"}, {"persistent_target": False},
            {"action_id": ""}, {"event": "death"},
        ]
        for metrics in invalid:
            with self.subTest(metrics=metrics):
                self.assertEqual(combat_progress_evidence(self.receipt(**metrics)), ())

    def test_hard_stops_and_missing_targets_cannot_borrow_old_health(self):
        for reason in ("preempted", "target_lost", "target_gone", "body_transport_error",
                       "governance_denied", "unknown"):
            with self.subTest(reason=reason):
                self.assertEqual(combat_progress_evidence(self.receipt(
                    success=False, reason=reason,
                )), ())


def state(pos=(0.04, 60, 0.04), health=20.04, food=20, inventory_hash="abc", time=999):
    return BodyState(
        bot="Bot1",
        pos=pos,
        yaw=None,
        pitch=None,
        health=health,
        food=food,
        oxygen=None,
        inventory_raw="[]",
        inventory_hash=inventory_hash,
        effects=None,
        time=time,
        weather=None,
        dimension=None,
        complete=True,
    )


class ProgressTests(unittest.TestCase):
    def test_fingerprint_canonicalizes_physical_state_but_ignores_time(self):
        progress = ProgressAuthority()

        a = progress.fingerprint(state())
        b = progress.fingerprint(state(pos=(0.03, 60, 0.04), health=20.03, time=998))
        c = progress.fingerprint(state(pos=(0.2, 60, 0.05)))
        d = progress.fingerprint(state(time=1000))

        self.assertEqual(a, b)
        self.assertNotEqual(c, a)
        self.assertEqual(d, a)

    def test_time_passing_does_not_reset_observation_stagnation(self):
        progress = ProgressAuthority()
        for index in range(STAGNATION_LIMIT + 1):
            progress.observe_step(("read_state",), progress.fingerprint(state(time=index * 1000)))
        self.assertTrue(progress.should_yield())

    def test_body_local_guard_uses_the_same_clock_independent_fingerprint(self):
        local = LocalProgressController()
        brain = ProgressAuthority()
        self.assertEqual(local.fingerprint(state(time=5000)), brain.fingerprint(state(time=1000)))


    def test_successful_tool_result_resets_stagnation_even_with_same_fingerprint(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        for _ in range(STAGNATION_LIMIT + 1):
            progress.note_step(("move_to", 1, 2, 3), success=True, fingerprint=fp)

        self.assertEqual(progress.stagnant_steps, 0)
        self.assertEqual(progress.stalled_steps, 0)
        self.assertEqual(progress.failure_steps, 0)
        self.assertFalse(progress.should_yield())


    def test_successful_varied_tool_results_reset_stall_even_with_same_fingerprint(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        for i in range(STALL_LIMIT + 1):
            progress.note_step(("action", i), success=True, fingerprint=fp)

        self.assertEqual(progress.stalled_steps, 0)
        self.assertEqual(progress.stagnant_steps, 0)
        self.assertEqual(progress.failure_steps, 0)
        self.assertFalse(progress.should_yield())


    def test_failure_storm_trips_at_limit(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        for i in range(FAILURE_STORM_LIMIT):
            progress.note_step(("action", i), success=False, fingerprint=fp)

        self.assertEqual(progress.failure_steps, FAILURE_STORM_LIMIT)
        with self.assertRaises(ProgressAbort) as cm:
            progress.require_can_continue("test goal")
        self.assertIsNotNone(cm.exception.facts)
        self.assertEqual(cm.exception.facts.goal, "test goal")
        self.assertEqual(cm.exception.facts.failure_steps, FAILURE_STORM_LIMIT)


    def test_neutral_preempted_does_not_increment_progress_sensors(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        for index in range(max(FAILURE_STORM_LIMIT, STAGNATION_LIMIT, STALL_LIMIT) + 1):
            progress.note_step(("move_to", index), success=False, fingerprint=fp, neutral=True)

        self.assertEqual(progress.failure_steps, 0)
        self.assertEqual(progress.stagnant_steps, 0)
        self.assertEqual(progress.stalled_steps, 0)
        self.assertFalse(progress.should_yield())

    def test_observation_step_counts_stagnation_without_failure_storm(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        for _ in range(STAGNATION_LIMIT + 1):
            progress.observe_step(("read_inventory", "{}"), fp)

        self.assertEqual(progress.stagnant_steps, STAGNATION_LIMIT)
        self.assertEqual(progress.failure_steps, 0)
        self.assertTrue(progress.should_yield())


    def test_generation_invalidation_makes_old_generation_stale(self):
        progress = ProgressAuthority()
        generation = progress.next_generation()

        self.assertTrue(progress.generation_current(generation))
        progress.invalidate_generation("lava_reflex")

        self.assertFalse(progress.generation_current(generation))

    def test_current_generation_captures_without_invalidating_active_owner(self):
        progress = ProgressAuthority()
        generation = progress.next_generation()

        captured = progress.current_generation()

        self.assertEqual(captured, generation)
        self.assertTrue(progress.generation_current(generation))

    def test_captured_steps_do_not_mutate_authority_until_committed(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        with progress.capture_steps() as captured:
            for index in range(FAILURE_STORM_LIMIT):
                progress.note_step(("action", index), success=False, fingerprint=fp)
                progress.require_can_continue("test goal")

        self.assertEqual(progress.failure_steps, 0)
        self.assertEqual(len(captured), FAILURE_STORM_LIMIT)

        with self.assertRaises(ProgressAbort):
            progress.commit_steps(captured, "test goal")

        self.assertEqual(progress.failure_steps, FAILURE_STORM_LIMIT)

    def test_nested_capture_contributes_steps_to_outer_epoch(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        with progress.capture_steps() as outer:
            progress.observe_step(("read_state", "first"), fp)
            with progress.capture_steps() as inner:
                progress.note_step(("move_to", 1), success=True, fingerprint=fp)

        self.assertEqual(len(inner), 1)
        self.assertEqual(len(outer), 2)
        self.assertIsNone(progress.last_action)

        progress.commit_steps(outer, "test goal")

        self.assertEqual(progress.last_action, ("move_to", 1))

    def test_novel_epistemic_evidence_defers_an_existing_failure_abort(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())
        for index in range(FAILURE_STORM_LIMIT - 1):
            progress.note_step(("action", index), success=False, fingerprint=fp)

        with progress.capture_steps() as captured:
            progress.note_step(("action", "terminal"), success=False, fingerprint=fp)

        progress.commit_steps(
            captured,
            "test goal",
            novel_epistemic_keys=("region:overworld:1,0",),
        )

        self.assertEqual(progress.failure_steps, FAILURE_STORM_LIMIT)
        self.assertEqual(progress.epistemic_steps, 1)
        self.assertEqual(progress.last_epistemic_keys, ("region:overworld:1,0",))

    def test_repeated_epistemic_evidence_does_not_reset_progress(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())
        for index in range(FAILURE_STORM_LIMIT - 1):
            progress.note_step(("action", index), success=False, fingerprint=fp)

        with progress.capture_steps() as captured:
            progress.note_step(("action", "terminal"), success=False, fingerprint=fp)

        progress.commit_steps(
            captured,
            "test goal",
            novel_epistemic_keys=("region:overworld:1,0",),
        )
        with self.assertRaises(ProgressAbort):
            progress.commit_steps((), "test goal", novel_epistemic_keys=())

        self.assertEqual(progress.epistemic_steps, 1)

    def test_epistemic_only_progress_has_a_bounded_window(self):
        progress = ProgressAuthority()

        for index in range(STALL_LIMIT - 1):
            progress.commit_steps(
                (),
                "test goal",
                novel_epistemic_keys=(f"region:{index}",),
            )

        with self.assertRaises(ProgressAbort) as cm:
            progress.commit_steps(
                (),
                "test goal",
                novel_epistemic_keys=(f"region:{STALL_LIMIT - 1}",),
            )

        self.assertEqual(cm.exception.facts.epistemic_steps, STALL_LIMIT)
        self.assertEqual(
            cm.exception.facts.last_epistemic_keys,
            (f"region:{STALL_LIMIT - 1}",),
        )

    def test_material_progress_resets_epistemic_window(self):
        progress = ProgressAuthority()
        progress.commit_steps(
            (),
            "test goal",
            novel_epistemic_keys=("region:one",),
        )
        progress.commit_steps(
            (),
            "test goal",
            novel_epistemic_keys=("region:two",),
        )

        progress.commit_steps((), "test goal", material_changed=True)

        self.assertEqual(progress.epistemic_steps, 0)
        self.assertEqual(progress.last_epistemic_keys, ())


if __name__ == "__main__":
    unittest.main()
