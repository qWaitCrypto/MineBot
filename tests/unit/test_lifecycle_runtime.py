import unittest

from minebot.body import LifecycleTransactions
from minebot.contract import BodyState, Event, Result


def state_at(pos=(0, 64, 0), *, missing=False):
    return BodyState(
        bot="Bot1",
        pos=(float(pos[0]), float(pos[1]), float(pos[2])),
        yaw=0.0,
        pitch=0.0,
        health=20.0,
        food=20,
        oxygen=None,
        inventory_raw="[]",
        inventory_hash="inv",
        effects=None,
        time=0,
        weather=None,
        dimension="overworld",
        complete=True,
        missing=missing,
    )


class FakeLifecycleBody:
    bot_name = "Bot1"

    def __init__(self, *, states, spawn_result, event_batches):
        self.states = list(states)
        self.spawn_result = spawn_result
        self.event_batches = [list(batch) for batch in event_batches]
        self.spawn_calls = []

    def get_state(self):
        if len(self.states) == 1:
            return self.states[0]
        return self.states.pop(0)

    def spawn(
        self,
        pos=None,
        timeout_s: float = 15.0,
        *,
        yaw=None,
        pitch=None,
        dimension=None,
        gamemode=None,
        emit_respawned: bool = False,
    ):
        self.spawn_calls.append(
            {
                "pos": pos,
                "timeout_s": timeout_s,
                "yaw": yaw,
                "pitch": pitch,
                "dimension": dimension,
                "gamemode": gamemode,
                "emit_respawned": emit_respawned,
            }
        )
        return self.spawn_result

    def poll_events(self):
        if not self.event_batches:
            return []
        return self.event_batches.pop(0)


class LifecycleRuntimeTests(unittest.TestCase):
    def test_recover_after_death_refuses_when_body_present(self):
        body = FakeLifecycleBody(
            states=[state_at((0, 64, 0), missing=False)],
            spawn_result=Result(
                id=None,
                bot="Bot1",
                type="result",
                ok=True,
                accepted=True,
                complete=True,
                data={"action": "spawn"},
                error=None,
            ),
            event_batches=[],
        )
        runtime = LifecycleTransactions(body)

        result = runtime.recover_after_death(respawn_pos=(3, 59, 0))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "body_not_missing")
        self.assertEqual(body.spawn_calls, [])

    def test_recover_after_death_respawns_missing_body_and_waits_for_event(self):
        body = FakeLifecycleBody(
            states=[
                state_at((0, -80, 0), missing=True),
                state_at((3, 59, 0), missing=False),
            ],
            spawn_result=Result(
                id=None,
                bot="Bot1",
                type="result",
                ok=True,
                accepted=True,
                complete=True,
                data={"action": "spawn"},
                error=None,
            ),
            event_batches=[
                [],
                [
                    Event(
                        seq=1,
                        tick=20,
                        bot="Bot1",
                        name="respawned",
                        data={"final_pos": [3.5, 59.0, 0.5]},
                    )
                ],
            ],
        )
        runtime = LifecycleTransactions(body)

        result = runtime.recover_after_death(respawn_pos=(3, 59, 0), yaw=90.0, pitch=0.0)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(body.spawn_calls[0]["pos"], (3, 59, 0))
        self.assertTrue(body.spawn_calls[0]["emit_respawned"])
        self.assertEqual(result.metrics["state_after"]["pos"], [3.0, 59.0, 0.0])

    def test_recover_after_death_reports_missing_respawn_event(self):
        body = FakeLifecycleBody(
            states=[
                state_at((0, -80, 0), missing=True),
                state_at((0, -80, 0), missing=True),
            ],
            spawn_result=Result(
                id=None,
                bot="Bot1",
                type="result",
                ok=True,
                accepted=True,
                complete=True,
                data={"action": "spawn"},
                error=None,
            ),
            event_batches=[[]],
        )
        runtime = LifecycleTransactions(body)

        result = runtime.recover_after_death(respawn_pos=(3, 59, 0), respawn_event_timeout_s=0.01)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "respawn_event_missing")

    def test_recover_after_death_accepts_state_recovery_when_respawn_event_missing(self):
        body = FakeLifecycleBody(
            states=[
                state_at((0, -80, 0), missing=True),
                state_at((3, 59, 0), missing=False),
            ],
            spawn_result=Result(
                id=None,
                bot="Bot1",
                type="result",
                ok=True,
                accepted=True,
                complete=True,
                data={"action": "spawn"},
                error=None,
            ),
            event_batches=[[]],
        )
        runtime = LifecycleTransactions(body)

        result = runtime.recover_after_death(respawn_pos=(3, 59, 0), respawn_event_timeout_s=0.01)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.metrics["respawn_event"], "missing_but_state_recovered")

    def test_recover_after_death_still_rejects_wrong_state_when_respawn_event_missing(self):
        body = FakeLifecycleBody(
            states=[
                state_at((0, -80, 0), missing=True),
                state_at((9, 59, 0), missing=False),
            ],
            spawn_result=Result(
                id=None,
                bot="Bot1",
                type="result",
                ok=True,
                accepted=True,
                complete=True,
                data={"action": "spawn"},
                error=None,
            ),
            event_batches=[[]],
        )
        runtime = LifecycleTransactions(body)

        result = runtime.recover_after_death(respawn_pos=(3, 59, 0), respawn_event_timeout_s=0.01)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "respawn_position_mismatch")
