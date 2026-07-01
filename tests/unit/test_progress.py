import unittest

from minebot.brain.progress import FAILURE_STORM_LIMIT, STAGNATION_LIMIT, STALL_LIMIT, ProgressAbort, ProgressAuthority
from minebot.contract import BodyState


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
    def test_fingerprint_canonicalizes_position_health_and_time_bucket(self):
        progress = ProgressAuthority()

        a = progress.fingerprint(state())
        b = progress.fingerprint(state(pos=(0.03, 60, 0.04), health=20.03, time=998))
        c = progress.fingerprint(state(pos=(0.2, 60, 0.05)))
        d = progress.fingerprint(state(time=1000))

        self.assertEqual(a, b)
        self.assertNotEqual(c, a)
        self.assertNotEqual(d, a)


    def test_stagnation_trips_on_same_action_same_fingerprint_at_limit(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        for _ in range(STAGNATION_LIMIT + 1):
            progress.note_step(("move_to", 1, 2, 3), success=True, fingerprint=fp)

        self.assertEqual(progress.stagnant_steps, STAGNATION_LIMIT)
        self.assertTrue(progress.should_yield())


    def test_stall_trips_on_varied_actions_same_fingerprint_at_limit(self):
        progress = ProgressAuthority()
        fp = progress.fingerprint(state())

        for i in range(STALL_LIMIT + 1):
            progress.note_step(("action", i), success=True, fingerprint=fp)

        self.assertEqual(progress.stalled_steps, STALL_LIMIT)
        self.assertTrue(progress.should_yield())


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


if __name__ == "__main__":
    unittest.main()
