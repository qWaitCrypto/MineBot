import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from minebot.app.runtime_state import (
    PlacementEdit,
    RUNTIME_SCHEMA_VERSION,
    RuntimeScope,
    RuntimeStateStore,
)


class BotPlacementTests(unittest.TestCase):
    def setUp(self):
        self.store = RuntimeStateStore(":memory:")
        self.addCleanup(self.store.close)
        self.scope = RuntimeScope("server", "world", "Bot1")
        self.edit = PlacementEdit(
            dimension="overworld", pos=(3, 64, -5), op="place",
            block_type="oak_planks", purpose="roof", action_id="action-1",
            instance="build-1", edit_index=0,
        )

    def observe(self, *edits, store=None, scope=None, call_id="call-1"):
        return (self.store if store is None else store).create_tool_observation(
            self.scope if scope is None else scope,
            tool_name="world_edit", tool_call_id=call_id,
            result={"success": True}, complete=True, placement_edits=edits,
        )

    def raw_rows(self):
        return [dict(row) for row in self.store._connection.execute(
            "SELECT * FROM bot_placements ORDER BY scope_key, dimension, x, y, z"
        ).fetchall()]

    def test_placement_edit_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            self.edit.purpose = "doorway"

    def test_v20_migration_keeps_existing_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            first = RuntimeStateStore(path)
            try:
                observation = self.observe(store=first)
                first._connection.execute("DROP TABLE bot_placements")
                first._connection.execute(
                    "UPDATE minebot_schema SET version = 20 WHERE singleton = 1"
                )
                first._connection.commit()
            finally:
                first.close()

            migrated = RuntimeStateStore(path)
            try:
                self.assertEqual(RUNTIME_SCHEMA_VERSION, 21)
                self.assertEqual(migrated.schema_version, 21)
                self.assertEqual(
                    migrated.get_tool_observation(self.scope, observation["handle"])["result"],
                    {"success": True},
                )
                self.assertEqual(migrated.list_bot_placements(self.scope), [])
                self.assertEqual(self.observe(self.edit, store=migrated)["placements_applied"], 1)
                self.assertEqual(len(migrated.list_bot_placements(self.scope)), 1)
            finally:
                migrated.close()

    def test_place_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            first = RuntimeStateStore(path)
            try:
                observation = self.observe(self.edit, store=first)
                expected = first.list_bot_placements(self.scope)
            finally:
                first.close()

            reopened = RuntimeStateStore(path)
            try:
                self.assertEqual(observation["placements_applied"], 1)
                self.assertEqual(reopened.list_bot_placements(self.scope), expected)
                self.assertEqual(expected, [{
                    "dimension": "overworld", "pos": [3, 64, -5],
                    "block_type": "oak_planks", "purpose": "roof",
                    "record_state": "confirmed", "source_action_id": "action-1",
                    "revision": 1, "placement_observation": observation["handle"],
                    "latest_observation": observation["handle"],
                    "updated_at": expected[0]["updated_at"],
                }])
                self.assertTrue(expected[0]["updated_at"])
            finally:
                reopened.close()

    def test_dimensions_and_world_scopes_are_isolated(self):
        other_scope = RuntimeScope("server", "other-world", "Bot1")
        self.observe(self.edit, replace(self.edit, dimension="the_nether"))
        self.observe(replace(self.edit, block_type="stone"), scope=other_scope)

        rows = self.store.list_bot_placements(self.scope)
        self.assertEqual({row["dimension"] for row in rows}, {"overworld", "the_nether"})
        self.assertTrue(all(row["block_type"] == "oak_planks" for row in rows))
        other_rows = self.store.list_bot_placements(other_scope)
        self.assertEqual(len(other_rows), 1)
        self.assertEqual(other_rows[0]["block_type"], "stone")

    def test_remove_preserves_history_and_updates_source(self):
        placed = self.observe(self.edit)
        removed = self.observe(replace(
            self.edit, op="remove", purpose="doorway", action_id="action-2",
            instance="reshape-1", edit_index=7,
        ))

        self.assertEqual(removed["placements_applied"], 1)
        self.assertEqual(self.store.list_bot_placements(self.scope), [])
        row, = self.store.list_bot_placements(self.scope, states=("removed",))
        self.assertEqual(row["record_state"], "removed")
        self.assertEqual(row["block_type"], "oak_planks")
        self.assertEqual(row["purpose"], "roof")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["placement_observation"], placed["handle"])
        self.assertEqual(row["latest_observation"], removed["handle"])
        raw, = self.raw_rows()
        self.assertEqual(raw["source_action_id"], "action-2")
        self.assertEqual(raw["source_instance"], "reshape-1")
        self.assertEqual(raw["source_edit_index"], 7)

    def test_remove_ignores_absent_removed_and_needs_check_rows(self):
        removal = replace(self.edit, op="remove", action_id="remove-1")
        self.assertEqual(self.observe(removal)["placements_applied"], 0)
        self.assertEqual(self.raw_rows(), [])
        self.observe(self.edit)
        self.observe(removal)
        before = self.raw_rows()
        self.assertEqual(self.observe(replace(removal, action_id="remove-2"))["placements_applied"], 0)
        self.assertEqual(self.raw_rows(), before)

        with self.store._connection:
            self.store._connection.execute("UPDATE bot_placements SET record_state = 'needs_check'")
        before = self.raw_rows()
        self.assertEqual(self.observe(replace(removal, action_id="remove-3"))["placements_applied"], 0)
        self.assertEqual(self.raw_rows(), before)
        self.assertEqual(len(self.store.list_bot_placements(self.scope, states=("needs_check",))), 1)

    def test_duplicate_and_older_same_action_edits_leave_all_fields_unchanged(self):
        newer = replace(self.edit, edit_index=5)
        self.observe(newer)
        before = self.raw_rows()
        for edit in (
            newer,
            replace(newer, block_type="stone", purpose="different", instance="retry"),
            replace(newer, op="remove", edit_index=4),
        ):
            with self.subTest(edit=edit):
                self.assertEqual(self.observe(edit)["placements_applied"], 0)
                self.assertEqual(self.raw_rows(), before)

    def test_edits_apply_in_order_and_reconfirm_with_new_placement_observation(self):
        first = self.observe(self.edit)
        removed = self.observe(replace(self.edit, op="remove", edit_index=1))
        self.assertEqual(removed["placements_applied"], 1)
        edits = (
            replace(self.edit, block_type="stone", purpose="doorway", edit_index=3),
            replace(self.edit, op="remove", edit_index=2),
            replace(self.edit, block_type="glass", purpose="window", edit_index=4),
        )
        latest = self.observe(*edits)
        self.assertEqual(latest["placements_applied"], 2)
        row, = self.store.list_bot_placements(self.scope)
        self.assertEqual(row["revision"], 4)
        self.assertEqual(row["block_type"], "glass")
        self.assertEqual(row["purpose"], "window")
        self.assertEqual(row["placement_observation"], latest["handle"])
        self.assertEqual(row["latest_observation"], latest["handle"])
        self.assertNotEqual(row["placement_observation"], first["handle"])

    def test_place_then_remove_in_one_observation(self):
        observation = self.observe(self.edit, replace(self.edit, op="remove", edit_index=1))
        self.assertEqual(observation["placements_applied"], 2)
        row, = self.store.list_bot_placements(self.scope, states=("confirmed", "removed"))
        self.assertEqual(row["record_state"], "removed")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["placement_observation"], observation["handle"])
        self.assertEqual(row["latest_observation"], observation["handle"])

    def test_different_action_or_missing_index_does_not_deduplicate(self):
        self.observe(replace(self.edit, edit_index=10))
        edits = (
            replace(self.edit, action_id="action-2", edit_index=0),
            replace(self.edit, action_id="action-2", edit_index=None),
            replace(self.edit, action_id="action-2", edit_index=0),
        )
        for edit in edits:
            self.assertEqual(self.observe(edit)["placements_applied"], 1)
        row, = self.store.list_bot_placements(self.scope)
        self.assertEqual(row["revision"], 4)
        self.assertEqual(row["source_action_id"], "action-2")

    def test_failed_edit_rolls_back_observation_and_all_placement_changes(self):
        self.observe(self.edit)
        before = self.raw_rows()
        observation_ids = self.store._connection.execute(
            "SELECT observation_id FROM tool_observations"
        ).fetchall()
        invalid = replace(self.edit, pos=(7, 64, 0), block_type=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.observe(
                replace(self.edit, block_type="stone", edit_index=1),
                replace(self.edit, pos=(6, 64, 0)),
                invalid, call_id="must-rollback",
            )
        self.assertEqual(self.raw_rows(), before)
        self.assertEqual(self.store._connection.execute(
            "SELECT observation_id FROM tool_observations"
        ).fetchall(), observation_ids)
        self.assertEqual(self.observe(replace(self.edit, edit_index=1))["placements_applied"], 1)

    def test_invalid_operation_raises_and_rolls_back(self):
        with self.assertRaises(ValueError):
            self.observe(self.edit, replace(self.edit, op="invalid"))
        self.assertEqual(self.raw_rows(), [])
        self.assertEqual(self.store._connection.execute(
            "SELECT count(*) FROM tool_observations"
        ).fetchone()[0], 0)

    def test_foreign_keys_are_enforced_and_scope_delete_cascades(self):
        self.observe(self.edit)
        self.assertEqual(self.store._connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        for column in ("scope_key", "placement_observation", "latest_observation"):
            with self.subTest(column=column):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY constraint failed"):
                    with self.store._connection:
                        self.store._connection.execute(
                            f"UPDATE bot_placements SET {column} = ?", ("missing",)
                        )
        self.assertEqual(self.store._connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.store._connection:
            self.store._connection.execute(
                "DELETE FROM runtime_scopes WHERE scope_key = ?", (self.scope.key,)
            )
        self.assertEqual(self.raw_rows(), [])

    def test_no_edits_and_empty_state_filter(self):
        self.assertEqual(self.observe()["placements_applied"], 0)
        self.observe(self.edit)
        self.assertEqual(self.store.list_bot_placements(self.scope, states=()), [])
        self.assertEqual(self.store.list_bot_placements(self.scope, states=("unknown",)), [])


if __name__ == "__main__":
    unittest.main()
