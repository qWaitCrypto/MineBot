"""F7 decision-replay corpus: pure assembly fixtures + committed-pack integrity.

brain-cognitive-framework.md §10.1. The assembly must be a pure function of a
trace event stream, tolerate partial epochs honestly, and every committed
fixture pack must stay loadable and schema-complete.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from minebot.brain.deliberation import DecisionContext
from minebot.brain.metacognition import (
    DecisionFixture,
    backfill_decision_context,
    fixtures_from_trace,
    tool_surface_digest,
)


CORPUS_DIR = Path("tests/fixtures/decision_corpus")


def _synthetic_trace() -> list[dict[str, object]]:
    return [
        {"event": "tool_manifest", "seq": 2, "tools": [{"name": "read_state"}, {"name": "move_to"}]},
        # --- epoch 1: two members, one unsettled -------------------------
        {"event": "llm_end", "seq": 10},
        {
            "event": "tool_invoke",
            "seq": 11,
            "tool": "read_state",
            "tool_call_id": "call-a",
            "arguments_summary": "{}",
        },
        {
            "event": "tool_decision_context",
            "seq": 12,
            "tool": "read_state",
            "tool_call_id": "call-a",
            "situational": "mobility",
            "lifecycle": "active",
            "policy_tags": ["mobility"],
            "tool_focus": ["navigation"],
            "last_known_body_state": {"pos": [0, 70, 0]},
            "recent_session_messages": [],
            "recent_tool_results": [],
        },
        {
            "event": "progress_epoch_opened",
            "seq": 13,
            "epoch_id": "epoch-1",
            "run_id": "run-1",
            "model_turn": 1,
            "member_tool_call_ids": ["call-a", "call-b"],
            "member_tools": ["read_state", "move_to"],
            "pre_body_fingerprint": "fp-pre",
        },
        {
            "event": "tool_result",
            "seq": 14,
            "tool": "read_state",
            "tool_call_id": "call-a",
            "success": True,
            "reason": "state_read",
            "model_result": {"success": True, "reason": "state_read"},
            "observation_handle": "observation:1",
        },
        {
            "event": "progress_epoch_settled",
            "seq": 15,
            "epoch_id": "epoch-1",
            "material_changed": False,
            "progress_aborted": False,
            "committed_progress_step_count": 1,
            "pre_body_fingerprint": "fp-pre",
            "post_body_fingerprint": "fp-post",
        },
        # --- epoch settled without a matching open must be skipped -------
        {"event": "progress_epoch_settled", "seq": 16, "epoch_id": "epoch-unknown"},
    ]


class FixtureAssemblyTests(unittest.TestCase):
    def test_one_fixture_per_settled_epoch_with_honest_unsettled_member(self) -> None:
        fixtures = fixtures_from_trace(_synthetic_trace(), source_run="synthetic-run")

        self.assertEqual(len(fixtures), 1)
        fixture = fixtures[0]
        self.assertEqual(fixture.fixture_id, "synthetic-run#epoch-1")
        self.assertEqual(fixture.source_run, "synthetic-run")
        self.assertEqual(fixture.seq, 13)
        self.assertEqual(fixture.decision_context, DecisionContext.MOBILITY.value)
        self.assertEqual(fixture.tool_surface_digest, tool_surface_digest([{"name": "read_state"}, {"name": "move_to"}]))
        # chosen preserves member order and carries arguments from tool_invoke
        self.assertEqual([item["tool"] for item in fixture.chosen], ["read_state", "move_to"])
        self.assertEqual(fixture.chosen[0]["arguments"], "{}")
        # settled: first member has its result; second is honestly unsettled
        self.assertEqual(fixture.settled[0]["reason"], "state_read")
        self.assertEqual(fixture.settled[1]["status"], "unsettled")
        self.assertEqual(fixture.outcome["post_body_fingerprint"], "fp-post")
        self.assertIs(fixture.labels["decision_context_backfilled"], True)
        # compiled context is the recorded facts, JSON-encoded and bounded
        compiled = json.loads(fixture.compiled_context)
        self.assertEqual(compiled["situational"], "mobility")

    def test_payload_round_trip_is_lossless(self) -> None:
        fixture = fixtures_from_trace(_synthetic_trace(), source_run="synthetic-run")[0]
        clone = DecisionFixture.from_payload(json.loads(json.dumps(fixture.to_payload())))
        self.assertEqual(clone, fixture)

    def test_backfill_classes_are_deterministic(self) -> None:
        self.assertIs(backfill_decision_context("mobility", "active"), DecisionContext.MOBILITY)
        self.assertIs(backfill_decision_context("death", "active"), DecisionContext.RECOVERY)
        self.assertIs(backfill_decision_context("normal", "recovering"), DecisionContext.RECOVERY)
        self.assertIs(backfill_decision_context("normal", "active"), DecisionContext.NORMAL)
        self.assertIs(backfill_decision_context(None, None), DecisionContext.NORMAL)


class CommittedPackIntegrityTests(unittest.TestCase):
    def test_committed_packs_exist_and_are_schema_complete(self) -> None:
        packs = sorted(CORPUS_DIR.glob("*.jsonl"))
        self.assertGreaterEqual(len(packs), 1, "P0 requires at least one harvested fixture pack")
        allowed_contexts = {context.value for context in DecisionContext}
        for pack in packs:
            count = 0
            for line in pack.read_text(encoding="utf-8").splitlines():
                fixture = DecisionFixture.from_payload(json.loads(line))
                count += 1
                self.assertTrue(fixture.fixture_id)
                self.assertTrue(fixture.source_run)
                self.assertIn(fixture.decision_context, allowed_contexts)
                self.assertTrue(fixture.tool_surface_digest)
                self.assertGreaterEqual(len(fixture.chosen), 1)
                self.assertEqual(len(fixture.settled), len(fixture.chosen))
                chosen_ids = [str(item.get("tool_call_id")) for item in fixture.chosen]
                settled_ids = [str(item.get("tool_call_id")) for item in fixture.settled]
                self.assertEqual(chosen_ids, settled_ids)
            self.assertGreater(count, 0, f"empty pack: {pack}")


if __name__ == "__main__":
    unittest.main()
