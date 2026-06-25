import json
import unittest

from minebot.game.errors import EnvelopeError, IncompletePayloadError, TruncatedPayloadError
from minebot.contract import Action
from minebot.game.protocol import (
    build_action_call,
    build_perceive_call,
    parse_events,
    parse_perception,
    parse_result,
    parse_state,
)


class ProtocolTests(unittest.TestCase):
    def test_build_action_call_uses_json_and_escapes_single_quotes(self):
        action = Action(id="a1", name="moveTo", params={"label": "Bob's base", "target": [1, 2, 3]})

        command = build_action_call("Bot1", action)

        self.assertTrue(command.startswith("script in minebot run minebot_action("))
        self.assertIn("\\'", command)
        self.assertIn('"name":"moveTo"', command)
        self.assertIn("Bob", command)

    def test_build_perceive_call_uses_scope_and_json_params(self):
        command = build_perceive_call("Bot1", "blockAt", {"x": 1, "y": 64, "z": 2})

        self.assertTrue(command.startswith("script in minebot run minebot_perceive("))
        self.assertIn("'Bot1'", command)
        self.assertIn("'blockAt'", command)
        self.assertIn('"x":1', command)


    def test_parse_result_requires_complete_envelope(self):
        raw = json.dumps(
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

        result = parse_result(" = " + raw)

        self.assertEqual(result.id, "a1")
        self.assertEqual(result.bot, "Bot1")
        self.assertTrue(result.ok)
        self.assertTrue(result.accepted)
        self.assertEqual(result.data["action"], "moveTo")

    def test_parse_result_allows_scarpet_timing_suffix(self):
        raw = (
            '{"type":"result","id":null,"bot":"Bot1","ok":true,"accepted":true,'
            '"complete":true,"data":{},"error":null} (1175µs)'
        )

        result = parse_result(" = " + raw)

        self.assertIsNone(result.id)
        self.assertEqual(result.bot, "Bot1")

    def test_parse_result_allows_scarpet_timing_prefix(self):
        raw = (
            '(144µs) {"type":"result","id":null,"bot":"Bot1","ok":true,"accepted":true,'
            '"complete":true,"data":{},"error":null}'
        )

        result = parse_result(" = " + raw)

        self.assertIsNone(result.id)
        self.assertEqual(result.bot, "Bot1")


    def test_parse_state_hashes_inventory_when_missing_hash(self):
        raw = json.dumps(
            {
                "type": "state",
                "bot": "Bot1",
                "ok": True,
                "complete": True,
                "data": {
                    "pos": [0.5, 60, 0.5],
                    "yaw": None,
                    "pitch": None,
                    "health": 20.0,
                    "food": 20,
                    "oxygen": None,
                    "inventory_raw": "[]",
                    "sleeping": False,
                    "effects": None,
                    "time": 12345,
                    "weather": None,
                    "dimension": None,
                },
                "error": None,
            }
        )

        state = parse_state(raw)

        self.assertEqual(state.pos, (0.5, 60.0, 0.5))
        self.assertTrue(state.inventory_hash)
        self.assertEqual(state.food, 20)
        self.assertFalse(state.sleeping)

    def test_parse_state_uses_server_inventory_hash_when_present(self):
        raw = json.dumps(
            {
                "type": "state",
                "bot": "Bot1",
                "ok": True,
                "complete": True,
                "data": {
                    "pos": [0.5, 60, 0.5],
                    "yaw": None,
                    "pitch": None,
                    "health": 20.0,
                    "food": 20,
                    "oxygen": None,
                    "inventory_raw": '[cobblestone, 16, {count:16,id:"minecraft:cobblestone"}]',
                    "inventory_hash": "server-hash-1",
                    "sleeping": True,
                    "effects": None,
                    "time": 12345,
                    "weather": None,
                    "dimension": None,
                },
                "error": None,
            }
        )

        state = parse_state(raw)

        self.assertEqual(state.inventory_hash, "server-hash-1")
        self.assertTrue(state.sleeping)
        self.assertEqual(
            state.inventory_raw,
            '[cobblestone, 16, {count:16,id:"minecraft:cobblestone"}]',
        )


    def test_parse_events_preserves_order_and_data(self):
        raw = json.dumps(
            {
                "type": "events",
                "bot": "Bot1",
                "ok": True,
                "complete": True,
                "next": None,
                "events": [
                    {"type": "event", "seq": 1, "tick": 10, "bot": "Bot1", "name": "moveStarted", "data": {}},
                    {
                        "type": "event",
                        "seq": 2,
                        "tick": 12,
                        "bot": "Bot1",
                        "name": "moveDone",
                        "data": {"action_id": "a1", "arrived": True},
                    },
                ],
                "error": None,
            }
        )

        events = parse_events(raw)

        self.assertEqual([e.name for e in events], ["moveStarted", "moveDone"])
        self.assertTrue(events[1].data["arrived"])

    def test_parse_events_allows_scarpet_load_banner_prefix(self):
        raw = (
            'minebot app loaded = {"type":"events","bot":"Bot1","ok":true,'
            '"complete":true,"next":null,"events":[],"error":null}'
        )

        events = parse_events(raw)

        self.assertEqual(events, [])

    def test_parse_perception_preserves_scope_data_and_uncertainty(self):
        raw = json.dumps(
            {
                "type": "perception",
                "bot": "Bot1",
                "scope": "blockAt",
                "ok": True,
                "complete": True,
                "data": {
                    "x": 1,
                    "y": 64,
                    "z": 2,
                    "type": "stone",
                    "state": "SOLID",
                    "properties": {"open": "false"},
                },
                "uncertainty": [],
                "next": None,
                "error": None,
            }
        )

        perception = parse_perception(raw)

        self.assertEqual(perception.scope, "blockAt")
        self.assertTrue(perception.ok)
        self.assertEqual(perception.data["state"], "SOLID")
        self.assertEqual(perception.data["properties"]["open"], "false")
        self.assertEqual(perception.uncertainty, [])

    def test_parse_perception_preserves_missing_body_failure(self):
        raw = json.dumps(
            {
                "type": "perception",
                "bot": "Bot1",
                "scope": "inventory",
                "ok": False,
                "complete": True,
                "data": {},
                "uncertainty": [{"reason": "missing_body"}],
                "next": None,
                "error": "missing_body",
            }
        )

        perception = parse_perception(raw)

        self.assertEqual(perception.scope, "inventory")
        self.assertFalse(perception.ok)
        self.assertTrue(perception.complete)
        self.assertEqual(perception.error, "missing_body")
        self.assertEqual(perception.uncertainty, [{"reason": "missing_body"}])


    def test_parse_rejects_truncated_payload_boundary(self):
        with self.assertRaises(TruncatedPayloadError):
            parse_result("x" * 4096)


    def test_parse_rejects_incomplete_payload(self):
        raw = json.dumps(
            {
                "type": "result",
                "id": "a1",
                "bot": "Bot1",
                "ok": True,
                "accepted": True,
                "complete": False,
                "data": {},
                "error": None,
            }
        )
        with self.assertRaises(IncompletePayloadError):
            parse_result(raw)

    def test_parse_perception_allows_bounded_incomplete_payload(self):
        raw = json.dumps(
            {
                "type": "perception",
                "bot": "Bot1",
                "scope": "nearbyBlocks",
                "ok": True,
                "complete": False,
                "data": {"blocks": []},
                "uncertainty": [{"reason": "limit_exceeded"}],
                "next": "limit",
                "error": None,
            }
        )

        perception = parse_perception(raw)

        self.assertFalse(perception.complete)
        self.assertEqual(perception.next, "limit")
        self.assertEqual(perception.uncertainty, [{"reason": "limit_exceeded"}])


    def test_parse_rejects_wrong_type(self):
        raw = json.dumps({"type": "state", "bot": "Bot1", "ok": True, "complete": True, "data": {}})
        with self.assertRaises(EnvelopeError):
            parse_result(raw)


if __name__ == "__main__":
    unittest.main()
