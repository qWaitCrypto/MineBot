import asyncio
import tempfile
import unittest
from pathlib import Path

from minebot.app.observation_artifacts import (
    ObservationPathError,
    PersistentToolObservationArchive,
    register_tool_observation_tools,
)
from minebot.app.runner import AgentRuntime, RuntimeRunContext, RuntimeTrace, sdk_tool_for
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController, LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult

from tests.unit.test_agent_runner_spine import FakeBody


def large_result() -> dict[str, object]:
    return ToolResult(
        True,
        "entities_found",
        False,
        metrics={
            "entities": [
                {"uuid": f"entity-{index}", "type": "husk", "distance": index + 0.5}
                for index in range(30)
            ],
            "counts": {"husk": 30},
            "diagnostic": "x" * 5000,
            "api_token": "must-not-persist",
        },
    ).to_payload()


class PersistentToolObservationArchiveTests(unittest.TestCase):
    def test_full_result_survives_reopen_is_sanitized_and_scope_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            first_scope = RuntimeScope("server", "world-a", "Bot1")
            second_scope = RuntimeScope("server", "world-b", "Bot1")
            store = RuntimeStateStore(path)
            archive = PersistentToolObservationArchive(store, first_scope)

            handle = archive.store(
                tool_name="find_hostiles",
                tool_call_id="tool-call-1",
                result=large_result(),
                complete=True,
            )
            record = store.get_tool_observation(first_scope, handle)

            self.assertEqual(len(record["result"]["metrics"]["entities"]), 30)
            self.assertEqual(record["result"]["metrics"]["api_token"], "<redacted>")
            self.assertGreater(record["payload_bytes"], 1000)
            self.assertIsNone(store.get_tool_observation(second_scope, handle))
            store.close()

            reopened_store = RuntimeStateStore(path)
            reopened = PersistentToolObservationArchive(reopened_store, first_scope)
            query = reopened.query(tool_name="find_hostiles")

            self.assertEqual(query["total_matches"], 1)
            self.assertEqual(query["results"][0]["handle"], handle)
            self.assertNotIn("result", query["results"][0])
            self.assertEqual(
                reopened.read(handle, path=["metrics", "counts"])["value"],
                {"husk": 30},
            )
            self.assertEqual(
                PersistentToolObservationArchive(reopened_store, second_scope).query()[
                    "total_matches"
                ],
                0,
            )
            reopened_store.close()

    def test_nested_list_and_long_string_are_pageable_without_losing_source(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        archive = PersistentToolObservationArchive(store, scope)
        handle = archive.store(
            tool_name="find_hostiles",
            tool_call_id="tool-call-2",
            result=large_result(),
            complete=True,
        )

        first = archive.read(handle, path=["metrics", "entities"], limit=7)
        last = archive.read(handle, path=["metrics", "entities"], start=28, limit=7)
        text = archive.read(
            handle,
            path=["metrics", "diagnostic"],
            start=0,
            max_chars=1000,
        )

        self.assertEqual(len(first["items"]), 7)
        self.assertEqual(first["next_start"], 7)
        self.assertFalse(first["complete"])
        self.assertEqual(len(last["items"]), 2)
        self.assertTrue(last["complete"])
        self.assertEqual(len(text["value"]), 1000)
        self.assertEqual(text["next_start"], 1000)
        with self.assertRaises(ObservationPathError):
            archive.read(handle, path=["metrics", "missing"])
        store.close()

    def test_registered_query_tools_return_handles_and_typed_path_failures(self):
        store = RuntimeStateStore(":memory:")
        archive = PersistentToolObservationArchive(
            store,
            RuntimeScope("server", "world", "Bot1"),
        )
        handle = archive.store(
            tool_name="find_hostiles",
            tool_call_id="tool-call-3",
            result=large_result(),
            complete=True,
        )
        registry = ToolRegistry()
        register_tool_observation_tools(registry, archive)

        query = registry.get("query_tool_observations").callable(
            {"tool": "find_hostiles", "limit": 5}
        )
        read = registry.get("read_tool_observation").callable(
            {"handle": handle, "path": ["metrics", "entities"], "limit": 3}
        )
        bad_path = registry.get("read_tool_observation").callable(
            {"handle": handle, "path": ["metrics", "missing"]}
        )

        self.assertEqual(query.metrics["results"][0]["handle"], handle)
        self.assertEqual(len(read.metrics["items"]), 3)
        self.assertEqual(bad_path.reason, "tool_observation_path_not_found")
        self.assertFalse(bad_path.can_retry)
        store.close()


class ToolObservationRuntimeIntegrationTests(unittest.TestCase):
    def test_sdk_projection_links_to_persisted_full_result(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        archive = PersistentToolObservationArchive(store, scope)
        tool = RegisteredTool(
            name="find_hostiles",
            description="Find hostiles",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=lambda _params: ToolResult(
                True,
                "entities_found",
                False,
                metrics=large_result()["metrics"],
            ),
            sidecar=ToolSidecar(
                progress_key="find_hostiles",
                mutating=False,
                tool_type="combat_perception",
            ),
        )
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="inspect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            observation_archive=archive,
        )
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=runtime.trace,
            runtime=runtime,
        )

        class Wrapper:
            context = runtime_context

        output = asyncio.run(sdk_tool_for(tool).on_invoke_tool(Wrapper(), "{}"))
        handle = output["observationHandle"]
        record = store.get_tool_observation(scope, handle)

        self.assertTrue(output["success"])
        self.assertFalse(output["projection"]["complete"])
        self.assertTrue(output["projection"]["queryable"])
        self.assertIn("metrics.entities", output["projection"]["omittedFields"])
        self.assertEqual(len(record["result"]["metrics"]["entities"]), 30)
        event = next(
            event for event in runtime.trace.snapshot() if event["event"] == "tool_result"
        )
        self.assertEqual(event["observation_handle"], handle)
        self.assertGreater(event["omitted_field_count"], 0)
        runtime.close()
        store.close()

    def test_archive_failure_is_visible_without_rewriting_body_truth(self):
        class BrokenArchive:
            def store(self, **_kwargs):
                raise OSError("disk unavailable")

        tool = RegisteredTool(
            name="read_state",
            description="Read state",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=lambda _params: ToolResult(
                True,
                "state_read",
                False,
                metrics={"pos": [1, 64, 2], "health": 20.0},
            ),
            sidecar=ToolSidecar(
                progress_key="read_state",
                mutating=False,
                tool_type="state",
            ),
        )
        trace = RuntimeTrace()
        runtime = AgentRuntime(
            body=FakeBody(),
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="inspect"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
            observation_archive=BrokenArchive(),
            trace=trace,
        )
        runtime_context = RuntimeRunContext(
            agent_context=runtime.agent_context,
            weld_context=runtime.weld_context,
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
            runtime=runtime,
        )

        class Wrapper:
            context = runtime_context

        output = asyncio.run(sdk_tool_for(tool).on_invoke_tool(Wrapper(), "{}"))

        self.assertTrue(output["success"])
        self.assertNotIn("observationHandle", output)
        self.assertFalse(output["projection"]["queryable"])
        self.assertTrue(
            any(event["event"] == "tool_observation_archive_failed" for event in trace.snapshot())
        )
        runtime.close()


if __name__ == "__main__":
    unittest.main()
