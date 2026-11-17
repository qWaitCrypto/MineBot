"""F7 drift analysis: pure comparison/aggregation + replay engine harness.

brain-cognitive-framework.md §10.1. The comparison logic must be
deterministic, classify all four verdicts, and the replay engine must turn
a scripted model response into ReplayedDecision records without any Body or
provider side effects.
"""

from __future__ import annotations

import asyncio
import unittest

from minebot.app.decision_replay import ReplayEngine, stub_tools_from_registry
from minebot.brain.metacognition import (
    DecisionFixture,
    ReplayedDecision,
    compare_decision,
    drift_report,
)
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult


def _fixture(
    fixture_id: str = "run#epoch-1",
    *,
    decision_context: str = "normal",
    chosen: tuple[dict[str, object], ...] = (
        {"tool_call_id": "a", "tool": "read_state", "arguments": "{}"},
        {"tool_call_id": "b", "tool": "move_to", "arguments": '{"x": 1, "y": 2}'},
    ),
) -> DecisionFixture:
    return DecisionFixture(
        fixture_id=fixture_id,
        source_run="run",
        seq=1,
        decision_context=decision_context,
        compiled_context='{"situational": "normal"}',
        tool_surface_digest="digest-old",
        chosen=chosen,
        settled=(),
        outcome={},
    )


class CompareDecisionTests(unittest.TestCase):
    def test_identical_when_tools_and_normalized_arguments_match(self) -> None:
        replay = ReplayedDecision(
            fixture_id="run#epoch-1",
            chosen=(
                # different order + different JSON key order must still match
                {"tool": "move_to", "arguments": '{"y": 2, "x": 1}'},
                {"tool": "read_state", "arguments": "{}"},
            ),
        )
        self.assertEqual(compare_decision(_fixture(), replay).verdict, "identical")

    def test_same_tools_when_arguments_differ(self) -> None:
        replay = ReplayedDecision(
            fixture_id="run#epoch-1",
            chosen=(
                {"tool": "read_state", "arguments": "{}"},
                {"tool": "move_to", "arguments": '{"x": 9, "y": 9}'},
            ),
        )
        self.assertEqual(compare_decision(_fixture(), replay).verdict, "same_tools")

    def test_divergent_when_tool_multiset_differs(self) -> None:
        replay = ReplayedDecision(
            fixture_id="run#epoch-1",
            chosen=({"tool": "read_state", "arguments": "{}"},),
        )
        comparison = compare_decision(_fixture(), replay)
        self.assertEqual(comparison.verdict, "divergent")
        self.assertEqual(comparison.replayed_tools, ("read_state",))

    def test_unreplayed_for_missing_or_errored_replay(self) -> None:
        self.assertEqual(compare_decision(_fixture(), None).verdict, "unreplayed")
        errored = ReplayedDecision(fixture_id="run#epoch-1", chosen=(), error="boom")
        comparison = compare_decision(_fixture(), errored)
        self.assertEqual(comparison.verdict, "unreplayed")
        self.assertEqual(comparison.error, "boom")

    def test_surface_digest_mismatch_is_reported_not_fatal(self) -> None:
        replay = ReplayedDecision(fixture_id="run#epoch-1", chosen=_fixture().chosen)
        comparison = compare_decision(_fixture(), replay, current_surface_digest="digest-new")
        self.assertFalse(comparison.surface_digest_match)
        self.assertEqual(comparison.verdict, "identical")


class DriftReportTests(unittest.TestCase):
    def test_aggregates_per_context_with_drift_rate_and_substitutions(self) -> None:
        fixtures = [
            _fixture("run#e1", decision_context="normal"),
            _fixture("run#e2", decision_context="normal"),
            _fixture("run#e3", decision_context="mobility"),
        ]
        replays = {
            "run#e1": ReplayedDecision("run#e1", fixtures[0].chosen),  # identical
            "run#e2": ReplayedDecision(
                "run#e2",
                (
                    {"tool": "read_state", "arguments": "{}"},
                    {"tool": "explore_for", "arguments": "{}"},  # move_to -> explore_for
                ),
            ),
            # mobility fixture left unreplayed
        }
        report = drift_report(fixtures, replays)

        self.assertEqual(report.total, 3)
        normal = report.by_context["normal"]
        self.assertEqual(normal["identical"], 1)
        self.assertEqual(normal["divergent"], 1)
        self.assertEqual(normal["drift_rate"], 0.5)
        mobility = report.by_context["mobility"]
        self.assertEqual(mobility["unreplayed"], 1)
        self.assertIsNone(mobility["drift_rate"])
        self.assertEqual(report.substitutions[0], ("move_to->explore_for", 1))
        payload = report.to_payload()
        self.assertEqual(payload["total"], 3)
        self.assertEqual(len(payload["comparisons"]), 3)


class ReplayEngineTests(unittest.TestCase):
    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for name in ("read_state", "move_to"):
            registry.register(
                RegisteredTool(
                    name,
                    f"{name} tool",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                    lambda params: ToolResult(True, "stub", False),
                    ToolSidecar(name, mutating=False),
                )
            )
        return registry

    def test_stub_tools_expose_real_framework_faces(self) -> None:
        tools = stub_tools_from_registry(self._registry())
        self.assertEqual([tool.name for tool in tools], ["read_state", "move_to"])

    def test_replay_extracts_scripted_tool_batch(self) -> None:
        class FakeCall:
            type = "function_call"

            def __init__(self, name: str, arguments: str) -> None:
                self.name = name
                self.arguments = arguments

        class Item:
            def __init__(self, raw) -> None:
                self.raw_item = raw

        class FakeResult:
            new_items = [
                Item(FakeCall("read_state", "{}")),
                Item(FakeCall("move_to", '{"x": 1}')),
            ]

        captured: dict[str, object] = {}

        async def fake_runner(agent, run_input, **kwargs):
            captured["agent"] = agent
            captured["input"] = run_input
            return FakeResult()

        engine = ReplayEngine(registry=self._registry(), runner_run=fake_runner)
        replay = asyncio.run(engine.replay(_fixture()))

        self.assertIsNone(replay.error)
        self.assertEqual(
            replay.chosen,
            (
                {"tool": "read_state", "arguments": "{}"},
                {"tool": "move_to", "arguments": '{"x": 1}'},
            ),
        )
        agent = captured["agent"]
        self.assertEqual(agent.tool_use_behavior, "stop_on_first_tool")
        self.assertIn('{"situational": "normal"}', agent.instructions)

    def test_provider_failure_becomes_unreplayed_data_not_a_crash(self) -> None:
        async def failing_runner(*args, **kwargs):
            raise RuntimeError("provider down")

        engine = ReplayEngine(registry=self._registry(), runner_run=failing_runner)
        replay = asyncio.run(engine.replay(_fixture()))

        self.assertEqual(replay.chosen, ())
        self.assertIn("provider down", replay.error or "")
        report = drift_report([_fixture()], {replay.fixture_id: replay})
        self.assertEqual(report.by_context["normal"]["unreplayed"], 1)


if __name__ == "__main__":
    unittest.main()
