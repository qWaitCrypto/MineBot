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


class CreeperFirstBody:
    """Nearest hostile is a creeper, then a zombie; records the radius asked."""
    bot_name = "Bot1"

    def __init__(self):
        self.radii = []

    def perceive(self, scope, params):
        self.radii.append(params.get("radius"))
        entities = [
            {"id": "c", "type": "minecraft:creeper", "name": "Creeper", "pos": [1.0, 64.0, 0.0], "dist2": 1.0},
            {"id": TARGET_UUID, "type": "minecraft:zombie", "name": "Zombie", "pos": [3.0, 64.0, 0.0], "dist2": 9.0},
        ]
        return PerceptionResult(bot="Bot1", scope=scope, type="perception", ok=True, complete=True,
                                data={"count": len(entities), "entities": entities}, uncertainty=[])


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
    def test_ranged_attack_uses_target_gone_for_any_target_without_health(self):
        class CrystalBody(FakeBody):
            observations = 0

            def perceive(self, scope, params):
                if scope != "nearbyEntities":
                    return super().perceive(scope, params)
                self.observations += 1
                entities = [] if self.observations > 1 else [{
                    "id": TARGET_UUID, "type": "minecraft:end_crystal", "name": "End Crystal",
                    "pos": [12.0, 64.0, 0.0], "dist2": 144.0,
                }]
                return PerceptionResult(
                    bot="Bot1", scope=scope, type="perception", ok=True, complete=True,
                    data={"entities": entities}, uncertainty=[],
                )

            def await_action_terminal(self, action_id, timeout_s=15.0, **kwargs):
                return Event(seq=1, tick=1, bot="Bot1", name="rangedDone", data={
                    "action_id": action_id, "success": False, "reason": "target_persisted",
                    "target_id": TARGET_UUID,
                })

        body = CrystalBody()
        result = CombatTransactions(body).ranged_attack(TARGET_UUID, timeout_s=5)
        self.assertTrue(result.success)
        self.assertTrue(result.metrics["target_gone"])
        self.assertEqual(result.metrics["target_id"], TARGET_UUID)
        self.assertEqual(body.actions[0].name, "rangedAttack")

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

    def test_a_fight_ended_beside_an_unseen_target_says_what_blocks_the_view(self):
        """easy run-5, 17:51: 20 s beside a spider with no swing; the stop says why."""
        from minebot.app.reflex_question import result_sentence
        from minebot.app.runner import _model_tool_payload
        body = FakeBody(terminal_reason="no_line_of_sight")
        terminal = body.await_action_terminal
        def unseen(*args, **kwargs):
            event = terminal(*args, **kwargs)
            event.data.update(target_distance=1.26, max_range=2.0,
                              sight_blocked_by={"block": "stone", "pos": [20, 78, -16]})
            return event
        body.await_action_terminal = unseen
        result = CombatTransactions(body).engage_entity("nearest_hostile", timeout_s=5)
        self.assertEqual(result.metrics["sight_blocked_by"]["pos"], [20, 78, -16])
        expected = ("engage_entity() has ended. It reported failure: no_line_of_sight. The target is 1 blocks away; "
                    "the call allowed 2. The stone at [20, 78, -16] is between the Body's eyes and the target.")
        self.assertEqual(result_sentence("engage_entity()", result.to_payload()), expected)
        projected = _model_tool_payload("engage_entity", result.to_payload(), trace_ref="unseen")
        self.assertEqual(result_sentence("engage_entity()", projected), expected)

    def test_a_fight_whose_walk_ended_short_says_how_far_the_target_is(self):
        from minebot.app.reflex_question import result_sentence
        body = FakeBody(terminal_reason="target_out_of_reach")
        terminal = body.await_action_terminal
        def short(*args, **kwargs):
            event = terminal(*args, **kwargs)
            event.data.update(target_distance=5.4, max_range=2.0)
            return event
        body.await_action_terminal = short
        result = CombatTransactions(body).engage_entity("nearest_hostile", timeout_s=5)
        self.assertEqual(result_sentence("engage_entity()", result.to_payload()),
                         "engage_entity() has ended. It reported failure: target_out_of_reach. "
                         "The target is 5 blocks away; the call allowed 2.")

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
        self.assertEqual(body.actions[0].params["disengage_health"], 0.0)

    def test_registered_tool_keeps_an_explicit_zero_disengage_health(self):
        from minebot.app.phase1_runtime import _engage_tool
        for params, expected in (({"target": "nearest_hostile", "disengage_health": 0}, 0.0),
                                 ({"target": "nearest_hostile"}, 0.0)):
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


class NearTargetTest(unittest.TestCase):
    """End baseline run-4: the Agent copied a 36-character crystal id wrongly and
    the shot found no target. A target is named by kind and where it was seen."""

    def _body(self):
        class Seen:
            def perceive(self, scope, params=None):
                return PerceptionResult(bot="Bot1", scope=scope, type="perception", ok=True, complete=True, data={
                    "entities": [
                        {"id": "11111111-1111-4111-8111-111111111111", "type": "minecraft:end_crystal",
                         "pos": [42.5, 77.0, 0.5], "dist2": 100.0},
                        {"id": "22222222-2222-4222-8222-222222222222", "type": "minecraft:end_crystal",
                         "pos": [-41.5, 98.0, -0.5], "dist2": 4000.0},
                    ]}, uncertainty=[])
        return Seen()

    def test_near_picks_the_one_seen_there_not_the_nearest(self):
        from minebot.body.combat import resolve_entity_target
        resolved = resolve_entity_target(self._body(), "end_crystal", radius=64, near=(-41.0, 97.0, 0.0))
        self.assertEqual(resolved.target_uuid, "22222222-2222-4222-8222-222222222222")
        nearest = resolve_entity_target(self._body(), "end_crystal", radius=64)
        self.assertEqual(nearest.target_uuid, "11111111-1111-4111-8111-111111111111")

    def test_a_creature_that_walked_a_few_blocks_from_where_it_was_seen_is_still_found(self):
        # easy run-19 06:38: a cow 8 blocks from the named position was "not found".
        from minebot.body.combat import resolve_entity_target
        resolved = resolve_entity_target(self._body(), "end_crystal", radius=64, near=(34.0, 77.0, 0.5))
        self.assertEqual(resolved.target_uuid, "11111111-1111-4111-8111-111111111111")

    def test_nothing_of_the_kind_near_the_named_position_says_where_one_is(self):
        from minebot.body.combat import resolve_entity_target
        result = resolve_entity_target(self._body(), "end_crystal", radius=64, near=(0.0, 70.0, 40.0))
        self.assertEqual(result.reason, "target_not_found")
        self.assertEqual(result.metrics["near"], [0.0, 70.0, 40.0])
        self.assertIn(result.metrics["nearest_of_kind_at"], ([42.5, 77.0, 0.5], [-41.5, 98.0, -0.5]))


def test_nearest_hostile_is_one_at_hand_and_never_a_creeper():
    # run-15 03:33:10: an Agent rule sent the Body at 2 health after a spider
    # 34 blocks off (body-tool-boundaries §13).
    from minebot.body.combat import NEAREST_HOSTILE_RADIUS, resolve_entity_target
    body = CreeperFirstBody()
    resolved = resolve_entity_target(body, "nearest_hostile", radius=64)
    assert resolved.target_uuid == TARGET_UUID
    assert body.radii == [NEAREST_HOSTILE_RADIUS]



class WithinTests(unittest.TestCase):
    """run-36 27:00: "the skeleton within 4 blocks is dead" walked the Body at 6.9 health
    toward a skeleton 18 blocks off; within ends such a call before a step is taken."""

    class SkeletonsBody(FakeBody):
        def perceive(self, scope, params):
            entities = [
                {"id": "22222222-2222-4222-8222-222222222222", "type": "minecraft:skeleton", "name": "Skeleton",
                 "pos": [18.0, 64.0, 0.0], "dist2": 324.0},
                {"id": TARGET_UUID, "type": "minecraft:skeleton", "name": "Skeleton",
                 "pos": [3.0, 64.0, 0.0], "dist2": 9.0},
            ][: self.shown]
            return PerceptionResult(bot="Bot1", scope=scope, type="perception", ok=True, complete=True,
                                    data={"count": len(entities), "entities": entities}, uncertainty=[])

    def test_none_within_ends_at_once_and_says_how_far_the_nearest_is(self):
        from minebot.app.reflex_question import result_sentence
        from minebot.app.runner import _model_tool_payload
        body = self.SkeletonsBody()
        body.shown = 1
        result = CombatTransactions(body).engage_entity("skeleton", within=4, timeout_s=5)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "none_within")
        self.assertEqual(body.actions, [])
        expected = ("engage_entity() has ended. It reported failure: none_within. "
                    "The target is 18 blocks away; the call allowed 4.")
        self.assertEqual(result_sentence("engage_entity()", result.to_payload()), expected)
        projected = _model_tool_payload("engage_entity", result.to_payload(), trace_ref="far")
        self.assertEqual(projected["summary"]["target_distance"], 18.0)
        self.assertEqual(projected["summary"]["max_range"], 4)

    def test_within_picks_the_one_inside(self):
        body = self.SkeletonsBody()
        body.shown = 2
        CombatTransactions(body).engage_entity("skeleton", within=4, timeout_s=5)
        self.assertEqual(body.actions[0].params["target_uuid"], TARGET_UUID)

    def test_the_tool_takes_within(self):
        from minebot.app.phase1_runtime import _engage_tool
        tool = _engage_tool(CombatTransactions(self.SkeletonsBody()))
        self.assertIn("within", tool.input_schema["properties"])
        self.assertIn("within", tool.description)
