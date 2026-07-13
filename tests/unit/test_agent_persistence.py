import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from minebot.app.conversation import PersistentWindowedConversationSession
from minebot.app.conversation_tools import register_conversation_archive_tools
from minebot.app.runtime_identity import (
    RuntimeIdentityError,
    parse_world_identity_response,
    resolve_runtime_scope,
)
from minebot.app.runtime_state import (
    CheckpointDisposition,
    CompletionAuthority,
    PlanStepStatus,
    RUNTIME_SCHEMA_VERSION,
    RuntimeScope,
    RuntimeStateError,
    RuntimeStateStore,
    TaskStatus,
)
from minebot.app.tasks import TaskWorkspace, register_task_tools
from minebot.brain.registry import ToolRegistry


def turn_items(index: int) -> list[dict[str, object]]:
    return [
        {"role": "user", "content": f"turn-{index}"},
        {"type": "function_call", "call_id": f"call-{index}", "name": "read_state"},
        {"type": "function_call_output", "call_id": f"call-{index}", "output": "ok"},
        {"role": "assistant", "content": f"done-{index}"},
    ]


class IdentityTransport:
    def __init__(self, world_id: str | None = None) -> None:
        self.world_id = world_id
        self.commands: list[str] = []

    def request(self, command: str) -> str:
        self.commands.append(command)
        if command.startswith("execute unless data storage"):
            if self.world_id is None:
                marker = 'set value "'
                self.world_id = command.split(marker, 1)[1].rsplit('"', 1)[0]
            return "Test passed"
        if command.startswith("data get storage"):
            if self.world_id is None:
                return "Found no elements matching world_id"
            return (
                "Storage minebot:runtime has the following contents: "
                + json.dumps(self.world_id)
            )
        raise AssertionError(command)


class RuntimeScopeTests(unittest.TestCase):
    def test_scope_key_is_stable_and_isolates_world_and_bot(self):
        scope = RuntimeScope("local", "world-a", "Bot1")

        self.assertEqual(scope.key, RuntimeScope("local", "world-a", "Bot1").key)
        self.assertNotEqual(scope.key, RuntimeScope("local", "world-b", "Bot1").key)
        self.assertNotEqual(scope.key, RuntimeScope("local", "world-a", "Bot2").key)
        self.assertTrue(scope.conversation_session_id.endswith(":conversation"))

    def test_scope_rejects_empty_and_control_characters(self):
        with self.assertRaises(ValueError):
            RuntimeScope("", "world", "Bot1")
        with self.assertRaises(ValueError):
            RuntimeScope("server", "world\nother", "Bot1")


class RuntimeStateStoreTests(unittest.TestCase):
    def test_store_reopens_registered_scope_and_has_foundation_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")

            first = RuntimeStateStore(path)
            first.register_scope(scope)
            self.assertEqual(first.schema_version, RUNTIME_SCHEMA_VERSION)
            first.close()

            second = RuntimeStateStore(path)
            self.assertTrue(second.has_scope(scope))
            table_names = {
                row[0]
                for row in second._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "runtime_scopes",
                    "tasks",
                    "task_plans",
                    "task_plan_steps",
                    "task_checkpoints",
                    "work_intents",
                    "event_cursors",
                    "conversation_archives",
                    "tool_observations",
                    "memory_entries",
                    "memory_fts_terms",
                    "memory_fts_trigrams",
                    "skill_activations",
                    "wiki_cache",
                }.issubset(table_names)
            )
            second.close()

    def test_store_refuses_unknown_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE minebot_schema (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO minebot_schema VALUES (1, 999)")
            connection.commit()
            connection.close()

            with self.assertRaises(RuntimeStateError):
                RuntimeStateStore(path)

    def test_store_migrates_v1_schema_to_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            first = RuntimeStateStore(path)
            first._connection.execute(
                "UPDATE minebot_schema SET version = 1 WHERE singleton = 1"
            )
            first._connection.commit()
            first.close()

            migrated = RuntimeStateStore(path)

            self.assertEqual(migrated.schema_version, RUNTIME_SCHEMA_VERSION)
            migrated.close()

    def test_store_migrates_v6_by_creating_tool_observation_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            first = RuntimeStateStore(path)
            first._connection.execute("DROP TABLE tool_observations")
            first._connection.execute(
                "UPDATE minebot_schema SET version = 6 WHERE singleton = 1"
            )
            first._connection.commit()
            first.close()

            migrated = RuntimeStateStore(path)
            table = migrated._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tool_observations'"
            ).fetchone()

            self.assertEqual(migrated.schema_version, RUNTIME_SCHEMA_VERSION)
            self.assertIsNotNone(table)
            migrated.close()


class TaskWorkspaceTests(unittest.TestCase):
    def test_task_plan_checkpoint_and_completion_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            store = RuntimeStateStore(path)
            workspace = TaskWorkspace(store, scope)

            task = workspace.start("prepare for the End", source="user_goal", requested_by="Steve")
            plan = workspace.update_plan(
                expected_revision=0,
                summary="Acquire and equip supplies",
                steps=[
                    {"title": "Acquire iron", "status": "in_progress", "evidence": []},
                    {"title": "Craft equipment", "status": "pending", "evidence": []},
                ],
            )
            refreshed = workspace.current_task
            self.assertIsNotNone(refreshed)
            checkpoint_task, checkpoint = workspace.checkpoint(
                expected_task_revision=refreshed.revision,
                disposition=CheckpointDisposition.WAIT_EVENT,
                summary="Waiting for furnace output",
                next_step="Collect ingots",
                evidence=["furnace input accepted"],
                wait_for=["furnace output available"],
            )

            self.assertEqual(task.status, TaskStatus.RUNNING)
            self.assertEqual(plan.revision, 1)
            self.assertEqual(plan.steps[0].status, PlanStepStatus.IN_PROGRESS)
            self.assertEqual(checkpoint_task.status, TaskStatus.WAITING_EVENT)
            self.assertEqual(checkpoint.disposition, CheckpointDisposition.WAIT_EVENT)
            store.close()

            reopened_store = RuntimeStateStore(path)
            reopened = TaskWorkspace(reopened_store, scope)
            payload = reopened.payload()

            self.assertTrue(payload["active"])
            self.assertEqual(payload["task"]["goal"], "prepare for the End")
            self.assertEqual(payload["plan"]["summary"], "Acquire and equip supplies")
            self.assertEqual(payload["checkpoint"]["wait_for"], ["furnace output available"])
            completed = reopened.complete(authority=CompletionAuthority.MODEL)
            self.assertEqual(completed.status, TaskStatus.COMPLETED)
            self.assertEqual(completed.completion_authority, CompletionAuthority.MODEL)
            self.assertFalse(reopened.payload()["active"])
            reopened_store.close()

    def test_only_one_foreground_task_and_replace_is_atomic(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = TaskWorkspace(store, scope)
        first = workspace.start("first", source="user")

        with self.assertRaises(RuntimeStateError):
            workspace.start("second", source="user")

        second = workspace.replace("second", source="user_replace")

        self.assertEqual(store.get_task(first.task_id).status, TaskStatus.CANCELLED)
        self.assertEqual(workspace.current_task.task_id, second.task_id)
        store.close()

    def test_plan_revision_and_single_in_progress_are_enforced(self):
        store = RuntimeStateStore(":memory:")
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        workspace.start("long task", source="user")
        plan = workspace.update_plan(
            expected_revision=0,
            summary="v1",
            steps=[{"title": "one", "status": "in_progress"}],
        )

        with self.assertRaises(RuntimeStateError):
            workspace.update_plan(
                expected_revision=0,
                summary="stale",
                steps=[{"title": "one", "status": "completed"}],
            )
        with self.assertRaises(ValueError):
            workspace.update_plan(
                expected_revision=plan.revision,
                summary="invalid",
                steps=[
                    {"title": "one", "status": "in_progress"},
                    {"title": "two", "status": "in_progress"},
                ],
            )
        store.close()

    def test_task_tools_are_shared_artifact_tools_with_revision_conflicts(self):
        store = RuntimeStateStore(":memory:")
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        workspace.start("long task", source="user")
        registry = ToolRegistry()
        register_task_tools(registry, workspace)

        first = registry.get("update_plan").callable(
            {
                "expected_revision": 0,
                "summary": "first",
                "steps": [{"title": "inspect", "status": "in_progress"}],
            }
        )
        stale = registry.get("update_plan").callable(
            {
                "expected_revision": 0,
                "summary": "stale",
                "steps": [],
            }
        )
        read = registry.get("read_task").callable({})

        self.assertTrue(first.success)
        self.assertFalse(stale.success)
        self.assertEqual(stale.reason, "task_plan_update_rejected")
        self.assertTrue(read.metrics["active"])
        self.assertEqual(set(registry.names()), {"read_task", "update_plan", "checkpoint_task"})
        store.close()


class PersistentConversationTests(unittest.TestCase):
    def test_conversation_survives_reopen_and_model_window_keeps_whole_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")

            first = PersistentWindowedConversationSession(
                scope.conversation_session_id,
                path,
                max_turns=2,
            )

            async def write_history():
                for index in range(3):
                    await first.add_items(turn_items(index))

            asyncio.run(write_history())
            first.close()

            second = PersistentWindowedConversationSession(
                scope.conversation_session_id,
                path,
                max_turns=2,
            )
            items = asyncio.run(second.get_items())
            limited_items = asyncio.run(second.get_items(limit=2))

            self.assertEqual(items[0]["content"], "turn-1")
            self.assertEqual(items[-1]["content"], "done-2")
            self.assertEqual(len(limited_items), 4)
            self.assertEqual(limited_items[0]["content"], "turn-2")
            self.assertEqual(limited_items[-1]["content"], "done-2")
            self.assertEqual(
                {item["call_id"] for item in items if item.get("type") == "function_call"},
                {item["call_id"] for item in items if item.get("type") == "function_call_output"},
            )
            second.close()

            connection = sqlite3.connect(path)
            archived_count = connection.execute(
                "SELECT COUNT(*) FROM agent_messages WHERE session_id = ?",
                (scope.conversation_session_id,),
            ).fetchone()[0]
            connection.close()
            self.assertEqual(archived_count, 12)

    def test_conversation_is_isolated_by_runtime_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            first_scope = RuntimeScope("server", "world-a", "Bot1")
            second_scope = RuntimeScope("server", "world-b", "Bot1")
            first = PersistentWindowedConversationSession(first_scope.conversation_session_id, path)
            second = PersistentWindowedConversationSession(second_scope.conversation_session_id, path)

            asyncio.run(first.add_items(turn_items(0)))

            self.assertEqual(asyncio.run(second.get_items()), [])
            first.close()
            second.close()

    def test_archive_survives_reopen_and_exposes_stable_paginated_turn_handles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            store = RuntimeStateStore(path)
            first = PersistentWindowedConversationSession(
                scope.conversation_session_id,
                path,
                max_turns=2,
                archive_store=store,
                scope=scope,
            )

            async def write_history():
                for index in range(4):
                    await first.add_items(turn_items(index))

            asyncio.run(write_history())
            summary = first.summary_payload()
            query = first.query_archive(query="turn-0", limit=1)
            handle = query["results"][0]["handle"]
            first_page = first.read_archive_turn(handle, start=0, limit=2)
            second_page = first.read_archive_turn(handle, start=2, limit=2)

            self.assertEqual(summary["total_closed_turns"], 4)
            self.assertEqual(summary["live_turns"], 2)
            self.assertEqual(summary["compacted_turns"], 2)
            self.assertEqual(summary["live_item_count"], 8)
            self.assertGreater(summary["live_item_chars"], 0)
            self.assertGreater(summary["archive_item_chars"], summary["live_item_chars"])
            self.assertEqual(summary["covered_turn_handles"][0], handle)
            self.assertEqual(query["total_matches"], 1)
            self.assertFalse(first_page["complete"])
            self.assertEqual(first_page["next_start"], 2)
            self.assertTrue(second_page["complete"])
            self.assertEqual(
                {item["call_id"] for item in [*first_page["items"], *second_page["items"]] if item.get("type") == "function_call"},
                {item["call_id"] for item in [*first_page["items"], *second_page["items"]] if item.get("type") == "function_call_output"},
            )
            first.close()
            store.close()

            reopened_store = RuntimeStateStore(path)
            reopened = PersistentWindowedConversationSession(
                scope.conversation_session_id,
                path,
                max_turns=2,
                archive_store=reopened_store,
                scope=scope,
            )
            asyncio.run(reopened.sync_archive())

            self.assertEqual(
                reopened.query_archive(query="turn-0")["results"][0]["handle"],
                handle,
            )
            self.assertEqual(reopened.read_archive_turn(handle)["item_count"], 4)
            reopened.close()
            reopened_store.close()

    def test_unclosed_or_mismatched_tool_turn_is_not_summarized_as_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            store = RuntimeStateStore(path)
            session = PersistentWindowedConversationSession(
                scope.conversation_session_id,
                path,
                max_turns=1,
                archive_store=store,
                scope=scope,
            )

            asyncio.run(session.add_items(turn_items(0)))
            asyncio.run(
                session.add_items(
                    [
                        {"role": "user", "content": "unfinished"},
                        {"type": "function_call", "call_id": "call-open", "name": "move_to"},
                        {"type": "function_call_output", "call_id": "wrong-call", "output": "{}"},
                    ]
                )
            )

            summary = session.summary_payload()
            query = session.query_archive()
            self.assertEqual(summary["total_closed_turns"], 1)
            self.assertEqual(summary["compacted_turns"], 0)
            self.assertEqual(query["total_matches"], 1)
            self.assertNotIn("unfinished", json.dumps(query, ensure_ascii=False))
            session.close()
            store.close()

    def test_archive_tools_return_queryable_results_and_scope_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            first_scope = RuntimeScope("server", "world-a", "Bot1")
            second_scope = RuntimeScope("server", "world-b", "Bot1")
            store = RuntimeStateStore(path)
            first = PersistentWindowedConversationSession(
                first_scope.conversation_session_id,
                path,
                archive_store=store,
                scope=first_scope,
            )
            second = PersistentWindowedConversationSession(
                second_scope.conversation_session_id,
                path,
                archive_store=store,
                scope=second_scope,
            )
            asyncio.run(first.add_items(turn_items(7)))
            registry = ToolRegistry()
            register_conversation_archive_tools(registry, first)

            query_result = registry.get("query_conversation_archive").callable(
                {"query": "turn-7", "limit": 1}
            )
            handle = query_result.metrics["results"][0]["handle"]
            read_result = registry.get("read_conversation_archive").callable(
                {"handle": handle, "limit": 2}
            )

            self.assertTrue(query_result.success)
            self.assertEqual(query_result.metrics["total_matches"], 1)
            self.assertTrue(read_result.success)
            self.assertEqual(read_result.metrics["item_count"], 4)
            self.assertEqual(second.query_archive()["total_matches"], 0)
            first.close()
            second.close()
            store.close()


class RuntimeIdentityTests(unittest.TestCase):
    def test_world_identity_is_initialized_once_and_persists(self):
        transport = IdentityTransport()

        first = resolve_runtime_scope(transport, server_id="local", bot_id="Bot1")
        second = resolve_runtime_scope(transport, server_id="local", bot_id="Bot1")

        self.assertEqual(first.world_id, second.world_id)
        self.assertTrue(first.world_id.startswith("world-"))
        self.assertEqual(
            len([command for command in transport.commands if command.startswith("data get storage")]),
            2,
        )

    def test_world_identity_override_does_not_touch_server_storage(self):
        transport = IdentityTransport()

        scope = resolve_runtime_scope(
            transport,
            server_id="local",
            bot_id="Bot1",
            world_id_override="fixture-w1",
        )

        self.assertEqual(scope.world_id, "fixture-w1")
        self.assertEqual(transport.commands, [])

    def test_invalid_world_identity_response_is_rejected(self):
        self.assertIsNone(parse_world_identity_response("Found no elements matching world_id"))
        self.assertIsNone(
            parse_world_identity_response(
                'Storage minebot:runtime has the following contents: "bad id with spaces"'
            )
        )
        with self.assertRaises(RuntimeIdentityError):
            resolve_runtime_scope(
                IdentityTransport("bad id with spaces"),
                server_id="local",
                bot_id="Bot1",
            )


if __name__ == "__main__":
    unittest.main()
