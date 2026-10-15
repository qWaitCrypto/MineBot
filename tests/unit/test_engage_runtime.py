"""Unit tests for CombatTransactions.engage_entity (S3 Python tool layer)."""

import unittest

from minebot.body.combat import CombatTransactions
from minebot.contract import Action, BodyState, Event, PerceptionResult, Result


TARGET_UUID = "11111111-1111-4111-8111-111111111111"


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

    def perceive(self, scope, params):
        entities = [{
            "id": TARGET_UUID,
            "type": "minecraft:zombie",
            "name": name,
            "pos": [1.0, 64.0, 0.0],
            "health": 20.0,
            "dist2": 1.0,
        } for name in ("Ghost", "Runner", "Brute", "X", "Zombie")]
        if scope == "nearbyHostiles":
            entities = entities[:1]
        elif scope != "nearbyEntities":
            raise AssertionError(f"unexpected scope {scope}")
        return PerceptionResult(
            bot="Bot1", scope=scope, type="perception", ok=True, complete=True,
            data={"count": len(entities), "entities": entities}, uncertainty=[],
        )

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
                "target_id": TARGET_UUID,
                "target_health": 0.0 if success else None,
                "target_initial_health": 20.0,
                "damage_observed": success,
                "persistent_target": True,
                "cooldown_ticks": 10,
                "min_attack_interval_ticks": 10 if success else None,
                "max_attack_interval_ticks": 10 if success else None,
            },
        )


class EngageRuntimeTests(unittest.TestCase):
    def test_native_broken_item_survives_python_result_and_agent_projection(self):
        from minebot.app.runner import _model_tool_payload
        body = FakeBody(terminal_reason="tool_broke")
        terminal = body.await_action_terminal
        def broken(*args, **kwargs):
            event = terminal(*args, **kwargs)
            event.data["broken_item"] = "wooden_sword"
            return event
        body.await_action_terminal = broken
        result = CombatTransactions(body).engage_entity("nearest_hostile", timeout_s=5)
        self.assertEqual(result.metrics["broken_item"], "wooden_sword")
        projected = _model_tool_payload("engage_entity", result.to_payload(), trace_ref="broken")
        self.assertEqual(projected["summary"]["broken_item"], "wooden_sword")

    def test_engage_entity_killed(self):
        body = FakeBody(terminal_reason="killed", terminal_attacks=5)
        runtime = CombatTransactions(body)

        result = runtime.engage_entity("nearest_hostile", attack_range=2.0, timeout_s=5.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "killed")
        self.assertEqual(len(body.actions), 1)
        self.assertEqual(body.actions[0].name, "engageEntity")
        self.assertEqual(body.actions[0].params["target_uuid"], TARGET_UUID)
        self.assertEqual(body.actions[0].params["attack_range"], 2.0)
        self.assertEqual(body.actions[0].params["disengage_health"], 6.0)

    def test_registered_tool_keeps_an_explicit_zero_disengage_health(self):
        from minebot.app.phase1_runtime import _engage_tool
        for params, expected in (({"target": "nearest_hostile", "disengage_health": 0}, 0.0),
                                 ({"target": "nearest_hostile"}, 6.0)):
            body = FakeBody(terminal_reason="killed", terminal_attacks=5)
            _engage_tool(CombatTransactions(body)).callable(params)
            self.assertEqual(body.actions[0].params["disengage_health"], expected)

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
        self.assertEqual(result.metrics["requested_target"], "Zombie")
        self.assertEqual(result.metrics["target_uuid"], TARGET_UUID)
        self.assertEqual(result.metrics["event"], "engageDone")
        self.assertEqual(result.metrics["attacks"], 7)
        self.assertEqual(result.metrics["target_id"], TARGET_UUID)
        self.assertTrue(result.metrics["damage_observed"])
        self.assertEqual(result.metrics["target_initial_health"], 20.0)
        self.assertEqual(result.metrics["action_id"], body.actions[0].id)
        self.assertEqual(result.metrics["min_attack_interval_ticks"], 10)

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
