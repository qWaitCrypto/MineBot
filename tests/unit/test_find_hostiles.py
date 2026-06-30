"""Unit tests for find_hostiles perception (S3)."""

import unittest

from minebot.body.combat import find_hostiles
from minebot.contract import PerceptionResult


class HostilesBody:
    def perceive(self, scope: str, params: dict[str, object]):
        if scope != "nearbyHostiles":
            raise AssertionError(f"unexpected scope {scope}")
        return PerceptionResult(
            bot="Bot1",
            scope="nearbyHostiles",
            type="perception",
            ok=True,
            complete=True,
            data={
                "count": 2,
                "entities": [
                    {"type": "zombie", "name": "", "pos": [1, 64, 1], "health": 20, "dist2": 3.0},
                    {"type": "skeleton", "name": "", "pos": [5, 64, 5], "health": 20, "dist2": 32.0},
                ],
            },
            uncertainty=[],
            next=None,
            error=None,
        )


class FindHostilesTests(unittest.TestCase):
    def test_find_hostiles_returns_entities_sorted(self):
        result = find_hostiles(HostilesBody(), radius=10, limit=8)
        self.assertTrue(result.success)
        self.assertEqual(result.reason, "hostiles_found")
        self.assertEqual(result.metrics["count"], 2)
        self.assertEqual(result.metrics["radius"], 10)
        types = [e["type"] for e in result.metrics["hostiles"]]
        self.assertEqual(types, ["zombie", "skeleton"])

    def test_find_hostiles_perception_failed(self):
        class BadBody:
            def perceive(self, scope, params):
                return PerceptionResult(
                    bot="Bot1", scope="nearbyHostiles", type="perception", ok=False,
                    complete=False, data={}, uncertainty=[], next=None, error="boom",
                )
        result = find_hostiles(BadBody(), radius=10)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "perception_failed")


if __name__ == "__main__":
    unittest.main()
