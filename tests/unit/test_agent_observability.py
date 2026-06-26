import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from minebot.app.observability import JsonlObservationSink, sanitize_observation
from minebot.app.runner import AgentRuntime, RuntimeTrace
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import ToolRegistry

from tests.unit.test_agent_runner_spine import FakeBody


class AgentObservabilityTests(unittest.TestCase):
    def test_jsonl_sink_writes_sanitized_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            sink = JsonlObservationSink(path)
            trace = RuntimeTrace(session_id="s1", sink=sink)

            trace.emit("provider_config", api_key="secret", nested={"auth_token": "jwt"}, ok=True)
            trace.close()

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session_id"], "s1")
            self.assertEqual(rows[0]["event"], "provider_config")
            self.assertEqual(rows[0]["api_key"], "<redacted>")
            self.assertEqual(rows[0]["nested"]["auth_token"], "<redacted>")
            self.assertTrue(rows[0]["ok"])

    def test_runtime_trace_records_body_state_events_and_turn_profile_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.jsonl"
            trace = RuntimeTrace(session_id="runtime-test", sink=JsonlObservationSink(path))
            body = FakeBody()

            async def fake_runner(*args, **kwargs):
                return {"ok": True}

            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=AgentContext(system_prompt="sys", goal_text="collect"),
                lifecycle=LifecycleController(),
                mode_runtime=ModeRuntime(),
                authority=ProgressAuthority(),
                runner_run=fake_runner,
                trace=trace,
            )

            asyncio.run(runtime.run_turn())
            trace.close()

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            events = [row["event"] for row in rows]
            self.assertIn("body_state", events)
            self.assertIn("body_events", events)
            self.assertIn("turn_profile", events)
            self.assertIn("turn_completed", events)
            self.assertTrue(all(row["session_id"] == "runtime-test" for row in rows))
            seqs = [row["seq"] for row in rows]
            self.assertEqual(seqs, sorted(seqs))

    def test_sanitize_observation_recurses_without_destroying_public_fields(self):
        safe = sanitize_observation(
            {
                "event": "tool_result",
                "password": "pw",
                "metrics": [{"token": "abc", "count": 3}],
            }
        )

        self.assertEqual(safe["event"], "tool_result")
        self.assertEqual(safe["password"], "<redacted>")
        self.assertEqual(safe["metrics"][0]["token"], "<redacted>")
        self.assertEqual(safe["metrics"][0]["count"], 3)


if __name__ == "__main__":
    unittest.main()
