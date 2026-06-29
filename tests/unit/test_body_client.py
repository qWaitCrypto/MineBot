import json
import unittest

from minebot.game.body import ScarpetBody
from minebot.contract import Action


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.commands = []

    def request(self, command: str) -> str:
        self.commands.append(command)
        if not self.responses:
            raise AssertionError("no fake response left")
        return self.responses.pop(0)


def envelope(payload):
    return json.dumps(payload)


class ActionThenEventTransport:
    def __init__(self, result_action: str, event_name: str, event_data: dict):
        self.result_action = result_action
        self.event_name = event_name
        self.event_data = dict(event_data)
        self.commands = []
        self.action_id = None

    def request(self, command: str) -> str:
        self.commands.append(command)
        if "minebot_action" in command:
            marker = '"id":"'
            start = command.index(marker) + len(marker)
            end = command.index('"', start)
            self.action_id = command[start:end]
            return envelope(
                {
                    "type": "result",
                    "id": self.action_id,
                    "bot": "Bot1",
                    "ok": True,
                    "accepted": True,
                    "complete": True,
                    "data": {"action": self.result_action},
                    "error": None,
                }
            )
        data = dict(self.event_data)
        data["action_id"] = self.action_id
        return envelope(
            {
                "type": "events",
                "bot": "Bot1",
                "ok": True,
                "complete": True,
                "next": None,
                "events": [
                    {
                        "type": "event",
                        "seq": 1,
                        "tick": 20,
                        "bot": "Bot1",
                        "name": self.event_name,
                        "data": data,
                    }
                ],
                "error": None,
            }
        )


class BodyClientTests(unittest.TestCase):
    def test_spawn_rejects_overlong_bot_name_before_transport(self):
        transport = FakeTransport([])
        body = ScarpetBody("E2EContainerViewBot", transport)

        result = body.spawn((0, 59, 0))

        self.assertFalse(result.ok)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error, "invalid_bot_name")
        self.assertEqual(result.data["reason"], "bot_name_too_long")
        self.assertEqual(result.data["max_length"], 16)
        self.assertEqual(transport.commands, [])

    def test_spawn_builds_positioned_payload(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "result",
                        "id": None,
                        "bot": "Bot1",
                        "ok": True,
                        "accepted": True,
                        "complete": True,
                        "data": {"action": "spawn"},
                        "error": None,
                    }
                ),
                envelope(
                    {
                        "type": "state",
                        "bot": "Bot1",
                        "ok": True,
                        "complete": True,
                        "data": {
                            "pos": [1.0, 80.0, 2.0],
                            "yaw": None,
                            "pitch": None,
                            "health": 20.0,
                            "food": 20,
                            "oxygen": None,
                            "inventory_raw": "[]",
                            "inventory_hash": "abc",
                            "effects": None,
                            "time": 0,
                            "weather": None,
                            "dimension": None,
                            "sleeping": None,
                            "missing": False,
                        },
                        "error": None,
                    }
                ),
            ]
        )
        body = ScarpetBody("Bot1", transport)

        result = body.spawn(
            (1, 80, 2),
            yaw=90.0,
            pitch=15.0,
            dimension="minecraft:the_end",
            gamemode="survival",
            emit_respawned=True,
            timeout_s=0.1,
        )

        self.assertTrue(result.ok)
        command = transport.commands[0]
        self.assertIn("minebot_spawn", command)
        self.assertIn('"pos":[1,80,2]', command)
        self.assertIn('"yaw":90.0', command)
        self.assertIn('"pitch":15.0', command)
        self.assertIn('"dimension":"minecraft:the_end"', command)
        self.assertIn('"gamemode":"survival"', command)
        self.assertIn('"emit_respawned":true', command)

    def test_scarpet_body_execute_builds_action_and_parses_result(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "result",
                        "id": "a1",
                        "bot": "Bot1",
                        "ok": True,
                        "accepted": True,
                        "complete": True,
                        "data": {"action": "moveTo"},
                        "error": None,
                    }
                )
            ]
        )
        body = ScarpetBody("Bot1", transport)

        result = body.execute(Action(id="a1", name="moveTo", params={"target": [1, 2, 3]}))

        self.assertTrue(result.accepted)
        self.assertIn("minebot_action", transport.commands[0])
        self.assertIn('"target":[1,2,3]', transport.commands[0])

    def test_scarpet_body_uses_transport_request_boundary(self):
        class RequestOnlyTransport:
            def __init__(self):
                self.command_seen = None

            def request(self, command: str) -> str:
                self.command_seen = command
                return envelope(
                    {
                        "type": "result",
                        "id": "a1",
                        "bot": "Bot1",
                        "ok": True,
                        "accepted": True,
                        "complete": True,
                        "data": {},
                        "error": None,
                    }
                )

        transport = RequestOnlyTransport()
        body = ScarpetBody("Bot1", transport)

        result = body.execute(Action(id="a1", name="moveTo", params={"target": [1, 2, 3]}))

        self.assertTrue(result.ok)
        self.assertIn("minebot_action", transport.command_seen)


    def test_poll_events_inserts_desync_fact_on_gap(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "events",
                        "bot": "Bot1",
                        "ok": True,
                        "complete": True,
                        "next": None,
                        "events": [
                            {"type": "event", "seq": 2, "tick": 20, "bot": "Bot1", "name": "moveDone", "data": {}}
                        ],
                        "error": None,
                    }
                )
            ]
        )
        body = ScarpetBody("Bot1", transport)

        events = body.poll_events()

        self.assertEqual([event.name for event in events], ["desync", "moveDone"])
        self.assertEqual(events[0].data, {"expected_seq": 1, "observed_seq": 2})
        snapshot = body.observability_snapshot()
        self.assertEqual(snapshot["transport"]["count"], 1)
        self.assertEqual(snapshot["events"][0]["name"], "desync")
        self.assertEqual(snapshot["events"][1]["name"], "moveDone")
        self.assertIn("minebot_events_since", transport.commands[0])
        self.assertIn("'0'", transport.commands[0])

    def test_poll_events_uses_retained_event_cursor(self):
        retained = envelope(
            {
                "type": "events",
                "bot": "Bot1",
                "ok": True,
                "complete": True,
                "next": None,
                "events": [
                    {"type": "event", "seq": 1, "tick": 10, "bot": "Bot1", "name": "moveStarted", "data": {}},
                    {"type": "event", "seq": 2, "tick": 20, "bot": "Bot1", "name": "moveDone", "data": {}},
                ],
                "error": None,
            }
        )
        transport = FakeTransport([retained, envelope({**json.loads(retained), "events": []})])
        body = ScarpetBody("Bot1", transport)

        first = body.poll_events()
        second = body.poll_events()

        self.assertEqual([event.seq for event in first], [1, 2])
        self.assertEqual(second, [])
        self.assertIn("'0'", transport.commands[0])
        self.assertIn("'2'", transport.commands[1])

    def test_poll_chat_events_uses_separate_chat_drain(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "events",
                        "bot": "Bot1",
                        "ok": True,
                        "complete": True,
                        "next": None,
                        "events": [
                            {
                                "type": "event",
                                "seq": 1,
                                "tick": 30,
                                "bot": "Bot1",
                                "name": "agentChat",
                                "data": {"sender": "Steve", "message": "collect 64 logs"},
                            }
                        ],
                        "error": None,
                    }
                )
            ]
        )
        body = ScarpetBody("Bot1", transport)

        events = body.poll_chat_events()

        self.assertEqual([event.name for event in events], ["agentChat"])
        self.assertEqual(events[0].data["sender"], "Steve")
        self.assertEqual(events[0].data["message"], "collect 64 logs")
        self.assertIn("minebot_chat_since", transport.commands[0])
        self.assertIn("'0'", transport.commands[0])

    def test_poll_chat_events_uses_retained_chat_cursor(self):
        retained = envelope(
            {
                "type": "events",
                "bot": "Bot1",
                "ok": True,
                "complete": True,
                "next": None,
                "events": [
                    {
                        "type": "event",
                        "seq": 4,
                        "tick": 40,
                        "bot": "Bot1",
                        "name": "agentChat",
                        "data": {"sender": "Steve", "message": "hello"},
                    }
                ],
                "error": None,
            }
        )
        transport = FakeTransport([retained, envelope({**json.loads(retained), "events": []})])
        body = ScarpetBody("Bot1", transport)

        first = body.poll_chat_events()
        second = body.poll_chat_events()

        self.assertEqual([event.seq for event in first], [4])
        self.assertEqual(second, [])
        self.assertIn("'0'", transport.commands[0])
        self.assertIn("'4'", transport.commands[1])


    def test_await_action_terminal_processes_batches_until_matching_move_done(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "events",
                        "bot": "Bot1",
                        "ok": True,
                        "complete": True,
                        "next": None,
                        "events": [
                            {"type": "event", "seq": 1, "tick": 10, "bot": "Bot1", "name": "moveStarted", "data": {}}
                        ],
                        "error": None,
                    }
                ),
                envelope(
                    {
                        "type": "events",
                        "bot": "Bot1",
                        "ok": True,
                        "complete": True,
                        "next": None,
                        "events": [
                            {
                                "type": "event",
                                "seq": 2,
                                "tick": 12,
                                "bot": "Bot1",
                                "name": "moveDone",
                                "data": {"action_id": "a1", "arrived": True},
                            }
                        ],
                        "error": None,
                    }
                ),
            ]
        )
        body = ScarpetBody("Bot1", transport)

        event = body.await_action_terminal("a1", timeout_s=1.0, poll_interval_s=0.0)

        self.assertEqual(event.name, "moveDone")
        self.assertTrue(event.data["arrived"])

    def test_execute_and_await_capture_action_trace(self):
        transport = ActionThenEventTransport(
            "moveTo",
            "moveDone",
            {"arrived": True, "stopped_reason": "arrived", "final_pos": [1.0, 59.0, 0.0]},
        )
        body = ScarpetBody("Bot1", transport)

        action = Action.create("moveTo", {"target": [1, 59, 0]})
        result = body.execute(action)
        self.assertTrue(result.accepted)
        event = body.await_action_terminal(action.id, timeout_s=1.0, poll_interval_s=0.0)

        self.assertEqual(event.name, "moveDone")
        snapshot = body.observability_snapshot()
        traces = snapshot["action_traces"]
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["action_id"], action.id)
        self.assertEqual(traces[0]["action_name"], "moveTo")
        self.assertEqual(traces[0]["terminal_event"], "moveDone")
        self.assertEqual(traces[0]["terminal_data"]["stopped_reason"], "arrived")
        self.assertGreaterEqual(traces[0]["wait_ms"], 0.0)
        self.assertGreaterEqual(snapshot["transport"]["count"], 2)

    def test_await_action_terminal_accepts_other_terminal_event_names(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "events",
                        "bot": "Bot1",
                        "ok": True,
                        "complete": True,
                        "next": None,
                        "events": [
                            {
                                "type": "event",
                                "seq": 1,
                                "tick": 12,
                                "bot": "Bot1",
                                "name": "mineDone",
                                "data": {"action_id": "a1", "completed": True},
                            }
                        ],
                        "error": None,
                    }
                )
            ]
        )
        body = ScarpetBody("Bot1", transport)

        event = body.await_action_terminal("a1", timeout_s=1.0, poll_interval_s=0.0)

        self.assertEqual(event.name, "mineDone")

    def test_await_action_terminal_accepts_instant_body_action_events(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "events",
                        "bot": "Bot1",
                        "ok": True,
                        "complete": True,
                        "next": None,
                        "events": [
                            {
                                "type": "event",
                                "seq": 1,
                                "tick": 12,
                                "bot": "Bot1",
                                "name": "lookDone",
                                "data": {"action_id": "a1", "success": True},
                            },
                            {
                                "type": "event",
                                "seq": 2,
                                "tick": 13,
                                "bot": "Bot1",
                                "name": "jumpDone",
                                "data": {"action_id": "a_jump", "success": True},
                            },
                            {
                                "type": "event",
                                "seq": 3,
                                "tick": 14,
                                "bot": "Bot1",
                                "name": "selectSlotDone",
                                "data": {"action_id": "a2", "success": True},
                            },
                        ],
                        "error": None,
                    }
                )
            ]
        )
        body = ScarpetBody("Bot1", transport)

        event = body.await_action_terminal("a2", timeout_s=1.0, poll_interval_s=0.0)

        self.assertEqual(event.name, "selectSlotDone")

    def test_jump_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "jump",
            "jumpDone",
            {
                "success": True,
                "final_pos": [1.0, 65.0, 2.0],
                "stopped_reason": "completed",
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.jump(timeout_s=1.0)

        self.assertEqual(event.name, "jumpDone")
        self.assertEqual(event.data["stopped_reason"], "completed")
        self.assertIn('"name":"jump"', transport.commands[0])

    def test_select_item_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "selectItem",
            "selectItemDone",
            {"success": True, "item": "minecraft:stone", "slot": 2, "count": 4},
        )
        body = ScarpetBody("Bot1", transport)

        event = body.select_item("minecraft:stone", timeout_s=1.0)

        self.assertEqual(event.name, "selectItemDone")
        self.assertEqual(event.data["slot"], 2)
        self.assertIn('"name":"selectItem"', transport.commands[0])
        self.assertIn('"item":"minecraft:stone"', transport.commands[0])

    def test_select_item_accepts_inventory_to_hotbar_terminal_event(self):
        transport = ActionThenEventTransport(
            "selectItem",
            "selectItemDone",
            {
                "success": True,
                "item": "minecraft:bread",
                "slot": 0,
                "count": 3,
                "stopped_reason": "moved_to_hotbar",
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.select_item("minecraft:bread", timeout_s=1.0)

        self.assertEqual(event.name, "selectItemDone")
        self.assertEqual(event.data["stopped_reason"], "moved_to_hotbar")
        self.assertEqual(event.data["slot"], 0)

    def test_use_item_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "useItem",
            "useDone",
            {
                "success": True,
                "mode": "continuous",
                "item": "minecraft:bow",
                "slot": 0,
                "ticks": 20,
                "inventory_before": "before",
                "inventory_after": "after",
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.use_item(mode="continuous", ticks=20, item="minecraft:bow", slot=0, timeout_s=1.0)

        self.assertEqual(event.name, "useDone")
        self.assertEqual(event.data["mode"], "continuous")
        self.assertIn('"name":"useItem"', transport.commands[0])
        self.assertIn('"mode":"continuous"', transport.commands[0])
        self.assertIn('"ticks":20', transport.commands[0])
        self.assertIn('"slot":0', transport.commands[0])

    def test_attack_entity_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "attackEntity",
            "attackDone",
            {
                "success": True,
                "target_type": "minecraft:husk",
                "target_id": "uuid-1",
                "target_name": "TestHusk",
                "target_health": 0,
                "target_initial_health": 20.0,
                "damage_observed": True,
                "persistent_target": True,
                "ticks": 34,
                "attacks": 4,
                "cooldown_ticks": 8,
                "min_attack_interval_ticks": 8,
                "max_attack_interval_ticks": 8,
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.attack_entity(target_type="minecraft:husk", target_name="TestHusk", radius=5, timeout_ticks=120, cooldown_ticks=8, timeout_s=1.0)

        self.assertEqual(event.name, "attackDone")
        self.assertEqual(event.data["attacks"], 4)
        self.assertIn('"name":"attackEntity"', transport.commands[0])
        self.assertIn('"target_type":"minecraft:husk"', transport.commands[0])
        self.assertIn('"target_name":"TestHusk"', transport.commands[0])
        self.assertIn('"radius":5', transport.commands[0])
        self.assertIn('"cooldown_ticks":8', transport.commands[0])

    def test_ranged_attack_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "rangedAttack",
            "rangedDone",
            {
                "success": True,
                "weapon": "bow",
                "target_type": "minecraft:husk",
                "target_id": "uuid-ranged-1",
                "target_name": "RangedHusk",
                "target_health": 11.0,
                "target_initial_health": 20.0,
                "damage_observed": True,
                "fired_observed": True,
                "ticks": 22,
                "use_interval_ticks": 22,
                "expected_shots": 1,
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.ranged_attack(
            weapon="bow",
            target_type="minecraft:husk",
            target_id="uuid-ranged-1",
            target_name="RangedHusk",
            radius=24,
            timeout_ticks=100,
            use_interval_ticks=22,
            expected_shots=1,
            timeout_s=1.0,
        )

        self.assertEqual(event.name, "rangedDone")
        self.assertTrue(event.data["damage_observed"])
        self.assertTrue(event.data["fired_observed"])
        self.assertIn('"name":"rangedAttack"', transport.commands[0])
        self.assertIn('"weapon":"bow"', transport.commands[0])
        self.assertIn('"target_type":"minecraft:husk"', transport.commands[0])
        self.assertIn('"target_id":"uuid-ranged-1"', transport.commands[0])
        self.assertIn('"target_name":"RangedHusk"', transport.commands[0])
        self.assertIn('"use_interval_ticks":22', transport.commands[0])

    def test_container_transfer_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "containerTransfer",
            "containerDone",
            {
                "success": True,
                "direction": "container_to_bot",
                "container_slot": 0,
                "bot_slot": 1,
                "item": "minecraft:diamond",
                "count": 3,
                "stopped_reason": "completed",
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.container_transfer(
            pos=(1, 59, 0),
            direction="container_to_bot",
            container_slot=0,
            bot_slot=1,
            count=2,
            timeout_s=1.0,
        )

        self.assertEqual(event.name, "containerDone")
        self.assertEqual(event.data["item"], "minecraft:diamond")
        self.assertIn('"name":"containerTransfer"', transport.commands[0])
        self.assertIn('"pos":[1,59,0]', transport.commands[0])
        self.assertIn('"direction":"container_to_bot"', transport.commands[0])
        self.assertIn('"bot_slot":1', transport.commands[0])
        self.assertIn('"count":2', transport.commands[0])

    def test_drop_item_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "dropItem",
            "dropDone",
            {
                "success": True,
                "slot": 2,
                "mode": "all",
                "item": "minecraft:cobblestone",
                "count_before": 16,
                "count_after": 0,
                "stopped_reason": "completed",
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.drop_item(slot=2, mode="all", timeout_s=1.0)

        self.assertEqual(event.name, "dropDone")
        self.assertEqual(event.data["count_after"], 0)
        self.assertIn('"name":"dropItem"', transport.commands[0])
        self.assertIn('"slot":2', transport.commands[0])
        self.assertIn('"mode":"all"', transport.commands[0])

    def test_move_item_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "moveItem",
            "moveItemDone",
            {
                "success": True,
                "from_slot": 9,
                "to_slot": 0,
                "item": "minecraft:stone",
                "count": 8,
                "stopped_reason": "completed",
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.move_item(from_slot=9, to_slot=0, count=4, timeout_s=1.0)

        self.assertEqual(event.name, "moveItemDone")
        self.assertEqual(event.data["to_slot"], 0)
        self.assertIn('"name":"moveItem"', transport.commands[0])
        self.assertIn('"from_slot":9', transport.commands[0])
        self.assertIn('"to_slot":0', transport.commands[0])
        self.assertIn('"count":4', transport.commands[0])

    def test_craft_item_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "craftItem",
            "craftDone",
            {
                "success": True,
                "item": "minecraft:oak_planks",
                "count": 4,
                "output_slot": 1,
                "stopped_reason": "completed",
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.craft_item(
            inputs=[{"slot": 0, "item": "minecraft:oak_log", "count": 1}],
            output={"slot": 1, "item": "minecraft:oak_planks", "count": 4},
            timeout_s=1.0,
        )

        self.assertEqual(event.name, "craftDone")
        self.assertEqual(event.data["item"], "minecraft:oak_planks")
        self.assertIn('"name":"craftItem"', transport.commands[0])
        self.assertIn('"inputs":[{"slot":0,"item":"minecraft:oak_log","count":1}]', transport.commands[0])
        self.assertIn('"output":{"slot":1,"item":"minecraft:oak_planks","count":4}', transport.commands[0])

    def test_furnace_transfer_executes_action_and_waits_for_terminal_event(self):
        transport = ActionThenEventTransport(
            "furnaceTransfer",
            "furnaceDone",
            {
                "success": True,
                "direction": "furnace_to_bot",
                "furnace_slot": "output",
                "furnace_slot_index": 2,
                "bot_slot": 3,
                "item": "minecraft:iron_ingot",
                "count": 2,
                "stopped_reason": "completed",
            },
        )
        body = ScarpetBody("Bot1", transport)

        event = body.furnace_transfer(
            pos=(2, 59, 0),
            direction="furnace_to_bot",
            furnace_slot="output",
            bot_slot=3,
            timeout_s=1.0,
        )

        self.assertEqual(event.name, "furnaceDone")
        self.assertEqual(event.data["item"], "minecraft:iron_ingot")
        self.assertIn('"name":"furnaceTransfer"', transport.commands[0])
        self.assertIn('"pos":[2,59,0]', transport.commands[0])
        self.assertIn('"direction":"furnace_to_bot"', transport.commands[0])
        self.assertIn('"furnace_slot":"output"', transport.commands[0])
        self.assertIn('"bot_slot":3', transport.commands[0])

    def test_get_inventory_pages_until_complete(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "perception",
                        "bot": "Bot1",
                        "scope": "inventory",
                        "ok": True,
                        "complete": False,
                        "data": {
                            "start": 0,
                            "limit": 1,
                            "nextStart": 1,
                            "totalSlots": 2,
                            "slots": [
                                {
                                    "slot": 0,
                                    "slotType": "hotbar",
                                    "slotLabel": "hotbar.0",
                                    "empty": False,
                                    "item": "minecraft:stone",
                                    "count": 3,
                                    "stackRaw": "{\"count\":3,\"id\":\"minecraft:stone\"}"
                                }
                            ],
                        },
                        "uncertainty": [{"reason": "page_limit"}],
                        "next": "1",
                        "error": None,
                    }
                ),
                envelope(
                    {
                        "type": "perception",
                        "bot": "Bot1",
                        "scope": "inventory",
                        "ok": True,
                        "complete": True,
                        "data": {
                            "start": 1,
                            "limit": 1,
                            "nextStart": None,
                            "totalSlots": 2,
                            "slots": [
                                {
                                    "slot": 1,
                                    "slotType": "hotbar",
                                    "slotLabel": "hotbar.1",
                                    "empty": True,
                                    "item": None,
                                    "count": 0,
                                    "stackRaw": None
                                }
                            ],
                        },
                        "uncertainty": [],
                        "next": None,
                        "error": None,
                    }
                ),
            ]
        )
        body = ScarpetBody("Bot1", transport)

        slots = body.get_inventory(page_size=1)

        self.assertEqual([slot.slot for slot in slots], [0, 1])
        self.assertEqual(slots[0].item, "minecraft:stone")
        self.assertEqual(slots[0].count, 3)
        self.assertEqual(slots[0].slot_type, "hotbar")
        self.assertEqual(slots[0].slot_label, "hotbar.0")
        self.assertEqual(slots[0].stack_raw, "{\"count\":3,\"id\":\"minecraft:stone\"}")
        self.assertTrue(slots[1].empty)
        self.assertEqual(slots[1].slot_type, "hotbar")
        self.assertEqual(slots[1].slot_label, "hotbar.1")
        self.assertIsNone(slots[1].stack_raw)
        self.assertEqual(len(transport.commands), 2)

    def test_get_container_pages_until_complete(self):
        transport = FakeTransport(
            [
                envelope(
                    {
                        "type": "perception",
                        "bot": "Bot1",
                        "scope": "container",
                        "ok": True,
                        "complete": False,
                        "data": {
                            "pos": [1, 59, 0],
                            "start": 0,
                            "limit": 1,
                            "nextStart": 1,
                            "totalSlots": 2,
                            "slots": [
                                {"slot": 0, "empty": False, "item": "minecraft:diamond", "count": 3}
                            ],
                        },
                        "uncertainty": [{"reason": "page_limit"}],
                        "next": "1",
                        "error": None,
                    }
                ),
                envelope(
                    {
                        "type": "perception",
                        "bot": "Bot1",
                        "scope": "container",
                        "ok": True,
                        "complete": True,
                        "data": {
                            "pos": [1, 59, 0],
                            "start": 1,
                            "limit": 1,
                            "nextStart": None,
                            "totalSlots": 2,
                            "slots": [
                                {"slot": 1, "empty": True, "item": None, "count": 0}
                            ],
                        },
                        "uncertainty": [],
                        "next": None,
                        "error": None,
                    }
                ),
            ]
        )
        body = ScarpetBody("Bot1", transport)

        slots = body.get_container((1, 59, 0), total_slots=2, page_size=1)

        self.assertEqual([slot.slot for slot in slots], [0, 1])
        self.assertEqual(slots[0].item, "minecraft:diamond")
        self.assertEqual(slots[0].count, 3)
        self.assertTrue(slots[1].empty)
        self.assertEqual(len(transport.commands), 2)
        self.assertIn("minebot_perceive", transport.commands[0])
        self.assertIn("'container'", transport.commands[0])
        self.assertIn('"pos":[1,59,0]', transport.commands[0])
        self.assertIn('"total_slots":2', transport.commands[0])


if __name__ == "__main__":
    unittest.main()
