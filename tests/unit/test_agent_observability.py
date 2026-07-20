import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from minebot.app.observability import JsonlObservationSink, sanitize_observation
from minebot.app.runner import AgentRuntime, RuntimeTrace
from minebot.app.real_server_session import TerminalTruth, safe_evaluate_terminal_truth
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
            raw_key = "sk-exampletokenvalue1234567890"

            trace.emit(
                "provider_config",
                api_key="secret",
                nested={"auth_token": "jwt"},
                recent_session_messages=[{"role": "user", "content": f"provider key: {raw_key}"}],
                ok=True,
            )
            trace.close()

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rendered = json.dumps(rows, sort_keys=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session_id"], "s1")
            self.assertEqual(rows[0]["event"], "provider_config")
            self.assertEqual(rows[0]["api_key"], "<redacted>")
            self.assertEqual(rows[0]["nested"]["auth_token"], "<redacted>")
            self.assertNotIn(raw_key, rendered)
            self.assertEqual(
                rows[0]["recent_session_messages"][0]["content"],
                "provider key: <redacted>",
            )
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

    def test_sanitize_observation_redacts_secret_values_embedded_in_text(self):
        raw_key = "sk-exampletokenvalue1234567890"
        raw_bearer = "abcdefghijklmnopqrstuvwxyz0123456789"
        safe = sanitize_observation(
            {
                "recent_session_messages": [
                    {"role": "user", "content": f"provider key: {raw_key}"},
                    {"role": "user", "content": f"Authorization: Bearer {raw_bearer}"},
                ],
                "summary": f"api_key={raw_key}",
            }
        )

        rendered = json.dumps(safe, sort_keys=True)
        self.assertNotIn(raw_key, rendered)
        self.assertNotIn(raw_bearer, rendered)
        self.assertEqual(safe["recent_session_messages"][0]["content"], "provider key: <redacted>")
        self.assertEqual(
            safe["recent_session_messages"][1]["content"],
            "Authorization: Bearer <redacted>",
        )
        self.assertEqual(safe["summary"], "api_key=<redacted>")

    def test_safe_terminal_truth_failure_is_logged_and_degrades_to_unsatisfied(self):
        class BrokenBody(FakeBody):
            def perceive(self, scope, params):
                if scope == "inventory":
                    raise RuntimeError("inventory truncated")
                return super().perceive(scope, params)

            def get_inventory(self):  # pragma: no cover - terminal truth must not call this path
                raise RuntimeError("inventory truncated")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.jsonl"
            trace = RuntimeTrace(session_id="terminal-test", sink=JsonlObservationSink(path))
            body = BrokenBody()

            async def fake_runner(*args, **kwargs):
                return {"ok": True}

            runtime = AgentRuntime(
                body=body,
                registry=ToolRegistry(),
                agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
                lifecycle=LifecycleController(),
                mode_runtime=ModeRuntime(),
                authority=ProgressAuthority(),
                runner_run=fake_runner,
                trace=trace,
            )
            asyncio.run(runtime.run_turn())

            class Parts:
                def __init__(self, runtime):
                    self.runtime = runtime

            class Session:
                def __init__(self, runtime):
                    self.parts = Parts(runtime)

            final = type("Final", (), {"status": "completed_turn", "lifecycle": type("L", (), {"value": "active"})()})()
            truth = safe_evaluate_terminal_truth(body, "collect 64 logs", final, session=Session(runtime))
            trace.close()

            self.assertIsInstance(truth, TerminalTruth)
            self.assertFalse(truth.satisfied)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row["event"] == "terminal_truth_failed" for row in rows))


if __name__ == "__main__":
    unittest.main()
