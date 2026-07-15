import tempfile
import unittest
from pathlib import Path

from minebot.app.exploration import PersistentExplorationCoverageStore
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.body import (
    CoverageStatus,
    ExplorationTargets,
    ExplorationTransactions,
    MemoryExplorationCoverageStore,
)
from minebot.contract import BodyState, PerceptionResult, ToolResult


def _state(pos=(0.0, 64.0, 0.0), *, health=20.0, missing=False):
    return BodyState(
        bot="Bot1",
        pos=pos,
        yaw=0.0,
        pitch=0.0,
        health=health,
        food=20,
        oxygen=300,
        inventory_raw="[]",
        inventory_hash="inventory",
        effects=[],
        time=0,
        weather="clear",
        dimension="minecraft:overworld",
        complete=True,
        missing=missing,
    )


def _region(pos):
    return (int(pos[0]) // 16, int(pos[2]) // 16)


class ExplorationBody:
    bot_name = "Bot1"

    def __init__(self, *, blocks=None, entities=None, scan_error=None):
        self.state = _state()
        self.blocks = dict(blocks or {})
        self.entities = dict(entities or {})
        self.scan_error = scan_error
        self.perceptions = []

    def get_state(self):
        return self.state

    def perceive(self, scope, params):
        self.perceptions.append((scope, dict(params)))
        region = _region(self.state.pos)
        if scope == "findBlocks":
            if self.scan_error:
                return PerceptionResult(
                    bot="Bot1",
                    scope=scope,
                    type="perception",
                    ok=False,
                    complete=False,
                    error=self.scan_error,
                    uncertainty=[{"reason": self.scan_error}],
                )
            return PerceptionResult(
                bot="Bot1",
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={"blocks": list(self.blocks.get(region, ()))},
            )
        if scope == "nearbyEntities":
            return PerceptionResult(
                bot="Bot1",
                scope=scope,
                type="perception",
                ok=True,
                complete=True,
                data={"entities": list(self.entities.get(region, ()))},
            )
        if scope == "blockCells":
            cells = list(params.get("cells") or ())
            start = int(params.get("start") or 0)
            limit = int(params.get("limit") or 128)
            page = cells[start : start + limit]
            facts = [
                {
                    "x": int(pos[0]),
                    "y": int(pos[1]),
                    "z": int(pos[2]),
                    "type": "stone" if int(pos[1]) == 63 else "air",
                    "state": "SOLID" if int(pos[1]) == 63 else "CLEAR",
                    "properties": {},
                }
                for pos in page
            ]
            next_start = start + len(page)
            complete = next_start >= len(cells)
            return PerceptionResult(
                bot="Bot1",
                scope=scope,
                type="perception",
                ok=True,
                complete=complete,
                data={
                    "cells": facts,
                    "count": len(facts),
                    "total": len(cells),
                    "nextStart": None if complete else next_start,
                },
                next=None if complete else str(next_start),
                uncertainty=[] if complete else [{"reason": "limit_exceeded"}],
            )
        raise AssertionError((scope, params))


class ExplorationNavigator:
    def __init__(self, body, *, outcomes=None):
        self.body = body
        self.outcomes = list(outcomes or ())
        self.calls = []

    def navigate_to(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        result = self.outcomes.pop(0) if self.outcomes else ToolResult(True, "arrived", False)
        if result.success and result.reason == "arrived":
            target = goal.representative((int(self.body.state.pos[0]), 64, int(self.body.state.pos[2])))
            self.body.state = _state(tuple(float(value) for value in target))
        return result


def _runtime(*, body=None, coverage=None, outcomes=None):
    body = body or ExplorationBody()
    navigator = ExplorationNavigator(body, outcomes=outcomes)
    return ExplorationTransactions(
        body,
        navigator,
        coverage or MemoryExplorationCoverageStore(),
    ), body, navigator


class ExplorationTransactionsTests(unittest.TestCase):
    def test_initial_region_returns_authoritative_block_match_without_navigation(self):
        body = ExplorationBody(
            blocks={(0, 0): [{"x": 2, "y": 64, "z": 3, "type": "oak_log", "state": "SOLID", "dist2": 13.0}]}
        )
        runtime, _, navigator = _runtime(body=body)

        result = runtime.explore_for(block_targets=("#logs",), max_regions=2)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "found")
        self.assertEqual(result.metrics["blocks"][0]["pos"], [2, 64, 3])
        self.assertFalse(navigator.calls)
        self.assertEqual(body.perceptions[0][1]["types"][0], "oak_log")

    def test_frontier_navigation_finds_target_in_next_region(self):
        body = ExplorationBody(
            blocks={(-1, -1): [{"x": -8, "y": 64, "z": -8, "type": "spruce_log", "state": "SOLID", "dist2": 1.0}]}
        )
        runtime, _, navigator = _runtime(body=body)

        result = runtime.explore_for(block_targets=("#logs",), max_regions=2)

        self.assertEqual(result.reason, "found")
        self.assertEqual(result.metrics["budget"]["regions_consumed"], 2)
        self.assertEqual(len(navigator.calls), 1)
        self.assertEqual(_region(body.state.pos), (-1, -1))
        block_cell_requests = [params for scope, params in body.perceptions if scope == "blockCells"]
        self.assertTrue(block_cell_requests)
        self.assertTrue(all(len(params["cells"]) <= 64 for params in block_cell_requests))

    def test_query_scoped_negative_coverage_is_not_rewalked(self):
        coverage = MemoryExplorationCoverageStore()
        runtime, body, navigator = _runtime(coverage=coverage)
        first = runtime.explore_for(block_targets=("#flowers",), max_regions=2)
        first_regions = set(tuple(item) for item in first.metrics["covered_regions"])
        first_call_count = len(navigator.calls)

        second = runtime.explore_for(block_targets=("#flowers",), max_regions=2)
        second_destinations = [
            _region(call[0].representative((int(body.state.pos[0]), 64, int(body.state.pos[2]))))
            for call in navigator.calls[first_call_count:]
        ]

        self.assertEqual(first.reason, "budget_exhausted")
        self.assertTrue(first_regions)
        self.assertTrue(first_regions.isdisjoint(second_destinations))
        self.assertGreater(second.metrics["coverage_revision"], first.metrics["coverage_revision"])

    def test_exact_region_budget_scans_only_current_region(self):
        runtime, _, navigator = _runtime()

        result = runtime.explore_for(block_targets=("dandelion",), max_regions=1)

        self.assertEqual(result.reason, "budget_exhausted")
        self.assertEqual(result.metrics["budget"]["regions_consumed"], 1)
        self.assertFalse(navigator.calls)
        self.assertIsNotNone(result.metrics["resume_cursor"])

    def test_candidate_attempt_exhaustion_is_mobility_blocked_not_budget_exhausted(self):
        outcomes = [ToolResult(False, "stuck", True) for _ in range(24)]
        runtime, _, navigator = _runtime(outcomes=outcomes)

        result = runtime.explore_for(block_targets=("dandelion",), max_regions=2)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "mobility_blocked")
        self.assertEqual(result.metrics["budget"]["regions_consumed"], 1)
        self.assertEqual(len(navigator.calls), 24)

    def test_scan_failure_keeps_prior_evidence_and_returns_unloaded_boundary(self):
        class FailingAfterMoveBody(ExplorationBody):
            def perceive(self, scope, params):
                if scope == "findBlocks" and _region(self.state.pos) != (0, 0):
                    self.scan_error = "chunk_unloaded"
                return super().perceive(scope, params)

        runtime, _, _ = _runtime(body=FailingAfterMoveBody())

        result = runtime.explore_for(block_targets=("dandelion",), max_regions=2)

        self.assertEqual(result.reason, "unloaded_boundary")
        self.assertEqual(result.metrics["covered_regions"], [[0, 0]])
        self.assertEqual(len(result.metrics["evidence_keys"]), 1)
        self.assertEqual(result.metrics["source_reason"], "unloaded_boundary")

    def test_preemption_is_typed_and_resumable(self):
        runtime, _, _ = _runtime(outcomes=[ToolResult(True, "preempted", True)])

        result = runtime.explore_for(block_targets=("dandelion",), max_regions=2)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "preempted")
        self.assertTrue(result.can_retry)
        self.assertIsNotNone(result.metrics["resume_cursor"])

    def test_death_supersedes_exploration(self):
        body = ExplorationBody()
        body.state = _state(health=0.0)
        runtime, _, _ = _runtime(body=body)

        result = runtime.explore_for(block_targets=("dandelion",))

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "death")

    def test_resume_cursor_rejects_target_mismatch(self):
        runtime, _, _ = _runtime()

        result = runtime.explore_for(
            block_targets=("dandelion",),
            resume_cursor={
                "query_signature": "wrong",
                "dimension": "minecraft:overworld",
                "coverage_revision": 0,
            },
        )

        self.assertEqual(result.reason, "exploration_resume_cursor_mismatch")

    def test_repeated_negative_scan_emits_stable_evidence_key(self):
        coverage = MemoryExplorationCoverageStore()
        runtime, _, _ = _runtime(coverage=coverage)
        first = runtime.explore_for(block_targets=("dandelion",), max_regions=1)
        second = runtime.explore_for(block_targets=("dandelion",), max_regions=1)

        self.assertEqual(first.metrics["evidence_keys"][0], second.metrics["evidence_keys"][0])
        self.assertEqual(second.metrics["budget"]["regions_consumed"], 1)

    def test_multiple_entity_targets_are_matched_in_one_scan(self):
        body = ExplorationBody(
            entities={(0, 0): [{"id": "pig-1", "type": "pig", "pos": [2.0, 64.0, 2.0], "health": 10.0, "dist2": 8.0}]}
        )
        runtime, _, _ = _runtime(body=body)

        result = runtime.explore_for(entity_targets=("#farm_animals",), max_regions=1)

        self.assertEqual(result.reason, "found")
        self.assertEqual(result.metrics["entities"][0]["id"], "pig-1")


class ExplorationCoveragePersistenceTests(unittest.TestCase):
    def test_coverage_survives_store_restart_and_remains_query_scoped(self):
        targets = ExplorationTargets.create(blocks=("#logs",), entities=())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            first_store = RuntimeStateStore(path)
            first = PersistentExplorationCoverageStore(first_store, scope)
            first.record_region(
                dimension="minecraft:overworld",
                query_signature=targets.query_signature,
                region=(2, -3),
                status=CoverageStatus.COVERED,
                center=(40, 64, -40),
                reason="authoritative_negative_scan",
                negative_evidence=("no_matching_blocks",),
            )
            first_store.close()

            second_store = RuntimeStateStore(path)
            second = PersistentExplorationCoverageStore(second_store, scope)
            regions = second.list_regions("minecraft:overworld", targets.query_signature)
            other = second.list_regions("minecraft:the_nether", targets.query_signature)

            self.assertEqual(len(regions), 1)
            self.assertEqual(regions[0].key, (2, -3))
            self.assertEqual(regions[0].negative_evidence, ("no_matching_blocks",))
            self.assertEqual(other, ())
            second_store.close()


if __name__ == "__main__":
    unittest.main()
