"""Unit tests for CombatTransactions.engage_entity (S3 Python tool layer)."""

import unittest

from minebot.body.combat import CombatTransactions
from minebot.contract import Action, BodyState, Event, Result


def state_at(pos):
    return BodyState(
        bot="Bot1",
        pos=(float(pos[0]), float(pos[1]), float(pos[2])),
        yaw=0.0,
        pitch=0.0,
        health=20.0,
        food=20,
        oxygen=300,
        inventory_raw="",
        inventory_hash="",
        effects=None,
        time=0,
        weather="clear",
        dimension="overworld",
        complete=True,
    )


class FakeBody:
    bot_name = "Bot1"

    def __init__(self, *, accept=True, terminal_reason=None, terminal_attacks=5):
        self.accept = accept
        self.terminal_reason = terminal_reason
        self.terminal_attacks = terminal_attacks
        self.actions: list[Action] = []
        self.await_timeouts: list[float] = []

    def get_state(self):
        return state_at((0, 64, 0))

    def execute(self, action: Action) -> Result:
        self.actions.append(action)
        return Result(
            id=action.id,
            bot="Bot1",
            type="result",
            ok=self.accept,
            accepted=self.accept,
            complete=True,
            data={"action": action.name},
            error=None if self.accept else "rejected",
        )

    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
        self.await_timeouts.append(timeout_s)
        reason = self.terminal_reason or "killed"
        success = reason == "killed"
        attacks = self.terminal_attacks if success else 0
        return Event(
            seq=1,
            tick=1,
            bot="Bot1",
            name="engageDone",
            data={
                "action_id": action_id,
                "success": success,
                "reason": reason,
                "attacks": attacks,
                "target_health": 0.0 if success else None,
            },
        )


class EngageRuntimeTests(unittest.TestCase):
    def test_engage_entity_killed(self):
        body = FakeBody(terminal_reason="killed", terminal_attacks=5)
        runtime = CombatTransactions(body)

        result = runtime.engage_entity("nearest_hostile", attack_range=2.0, timeout_s=5.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "killed")
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "engageEntity")
        self.assertEqual(body.actions[0].params["target_spec"], "nearest_hostile")
        self.assertEqual(body.actions[0].params["attack_range"], 2.0)
        self.assertEqual(body.actions[0].params["disengage_health"], 6.0)

    def test_engage_entity_target_lost(self):
        body = FakeBody(terminal_reason="target_lost", terminal_attacks=0)
        result = CombatTransactions(body).engage_entity("Ghost", timeout_s=5.0)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "target_lost")
        self.assertTrue(result.can_retry)

    def test_engage_entity_timeout(self):
        body = FakeBody(terminal_reason="timeout", terminal_attacks=2)
        result = CombatTransactions(body).engage_entity("Runner", timeout_s=5.0)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "timeout")

    def test_engage_entity_disengaged_low_health(self):
        body = FakeBody(terminal_reason="disengaged_low_health", terminal_attacks=1)
        result = CombatTransactions(body).engage_entity("Brute", timeout_s=5.0)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "disengaged_low_health")
        self.assertTrue(result.can_retry)

    def test_engage_entity_body_rejected(self):
        body = FakeBody(accept=False)
        result = CombatTransactions(body).engage_entity("X", timeout_s=5.0)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "body_rejected")

    def test_engage_entity_rejects_bad_params(self):
        runtime = CombatTransactions(FakeBody())
        with self.assertRaises(ValueError):
            runtime.engage_entity("", timeout_s=5.0)
        with self.assertRaises(ValueError):
            runtime.engage_entity("X", timeout_s=0)
        with self.assertRaises(ValueError):
            runtime.engage_entity("X", attack_range=1.0, timeout_s=5.0)
        with self.assertRaises(ValueError):
            runtime.engage_entity("X", disengage_health=-1, timeout_s=5.0)

    def test_engage_entity_metrics_carry_target_and_attacks(self):
        body = FakeBody(terminal_reason="killed", terminal_attacks=7)
        result = CombatTransactions(body).engage_entity("Zombie", timeout_s=5.0)
        self.assertEqual(result.metrics["target_spec"], "Zombie")
        self.assertEqual(result.metrics["event"], "engageDone")
        self.assertEqual(result.metrics["attacks"], 7)

    def test_engage_entity_awaits_engage_done_terminal(self):
        body = FakeBody(terminal_reason="killed")
        CombatTransactions(body).engage_entity("X", timeout_s=5.0)
        self.assertEqual(body.await_timeouts, [10.0])  # timeout_s + 5.0

    def test_engage_entity_does_not_invalidate_outer_generation_when_shared(self):
        from minebot.brain.progress import ProgressAuthority

        progress = ProgressAuthority()
        outer_generation = progress.next_generation()
        body = FakeBody(terminal_reason="killed")

        result = CombatTransactions(body, progress=progress).engage_entity("nearest_hostile", timeout_s=5.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "killed")
        self.assertTrue(progress.generation_current(outer_generation))

    def test_engage_entity_returns_preempted_when_generation_stale(self):
        from minebot.brain.progress import ProgressAuthority

        progress = ProgressAuthority()

        class PreemptingBody(FakeBody):
            def await_action_terminal(self, action_id: str, timeout_s: float = 15.0, **kwargs) -> Event:
                progress.invalidate_generation("test_preempt")
                return super().await_action_terminal(action_id, timeout_s=timeout_s, **kwargs)

        result = CombatTransactions(PreemptingBody(terminal_reason="killed"), progress=progress).engage_entity(
            "nearest_hostile", timeout_s=5.0
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "preempted")


if __name__ == "__main__":
    unittest.main()
