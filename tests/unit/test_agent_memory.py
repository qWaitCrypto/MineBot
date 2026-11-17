import json
import tempfile
import unittest
from pathlib import Path

from minebot.app.memory import MemoryWorkspace, register_memory_tools
from minebot.app.runtime_state import (
    MemoryKind,
    MemorySource,
    MemoryStateConflict,
    RuntimeScope,
    RuntimeStateStore,
)
from minebot.brain.registry import ToolRegistry


FIXTURE = Path(__file__).parents[1] / "fixtures" / "memory_retrieval_judgments.json"


def _write_entry(workspace: MemoryWorkspace, raw: dict[str, object]):
    return workspace.write(
        kind=MemoryKind(str(raw["kind"])),
        source=MemorySource(str(raw["source"])),
        title=str(raw["title"]),
        content=str(raw["content"]),
        subject_key=str(raw["subject_key"]),
        evidence_ref=str(raw.get("evidence_ref") or ""),
        dimension=str(raw.get("dimension") or "") or None,
        point=None if raw.get("point") is None else tuple(raw["point"]),
        region=None if raw.get("region") is None else tuple(raw["region"]),
    )


class MemoryPersistenceTests(unittest.TestCase):
    def test_memory_survives_reopen_and_never_crosses_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world-a", "Bot1")
            other_world = RuntimeScope("server", "world-b", "Bot1")
            store = RuntimeStateStore(path)
            memory = MemoryWorkspace(store, scope).write(
                kind=MemoryKind.EPISODIC,
                source=MemorySource.PLAYER_TOLD,
                title="Meet at dawn",
                content="Steve asked to meet beside the eastern gate.",
                subject_key="request:meet",
                evidence_ref="conversation:1",
            )
            store.close()

            reopened = RuntimeStateStore(path)
            self.assertEqual(
                MemoryWorkspace(reopened, scope).read(memory.memory_id).content,
                "Steve asked to meet beside the eastern gate.",
            )
            self.assertIsNone(MemoryWorkspace(reopened, other_world).read(memory.memory_id))
            self.assertEqual(
                MemoryWorkspace(reopened, other_world).search({"query": "eastern gate"})[
                    "candidate_count"
                ],
                0,
            )
            reopened.close()

    def test_update_removes_stale_fts_terms_and_enforces_source_precedence(self):
        store = RuntimeStateStore(":memory:")
        workspace = MemoryWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        memory = workspace.write(
            kind=MemoryKind.SPATIAL,
            source=MemorySource.OBSERVED,
            title="Portal location",
            content="The portal stands beside the old spruce tower.",
            subject_key="place:portal",
            evidence_ref="toolobs:1",
            dimension="overworld",
            point=(1, 64, 2),
        )

        updated = workspace.update(
            memory.memory_id,
            expected_revision=memory.revision,
            changes={
                "content": "The portal now stands beside the new stone tower.",
                "evidence_ref": "toolobs:2",
            },
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(workspace.search({"query": "spruce"})["candidate_count"], 0)
        self.assertEqual(workspace.search({"query": "stone tower"})["candidate_count"], 1)
        with self.assertRaises(MemoryStateConflict):
            workspace.update(
                memory.memory_id,
                expected_revision=updated.revision,
                changes={"source": "self_inferred", "content": "Maybe elsewhere"},
            )
        store.close()

    def test_delete_removes_search_index_and_requires_revision(self):
        store = RuntimeStateStore(":memory:")
        workspace = MemoryWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        memory = workspace.write(
            kind=MemoryKind.REFLECTIVE,
            source=MemorySource.SELF_INFERRED,
            title="Bad route",
            content="This route repeatedly enters a water pit.",
        )
        with self.assertRaises(MemoryStateConflict):
            workspace.delete(memory.memory_id, expected_revision=99)
        workspace.delete(memory.memory_id, expected_revision=memory.revision)
        self.assertEqual(workspace.search({"query": "water pit"})["candidate_count"], 0)
        store.close()


class MemoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.store = RuntimeStateStore(":memory:")
        self.workspace = MemoryWorkspace(
            self.store,
            RuntimeScope("server", "world", "Bot1"),
        )
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for raw in self.fixture["entries"]:
            _write_entry(self.workspace, raw)

    def tearDown(self):
        self.store.close()

    def test_judgment_set_meets_recall_and_mrr_gate(self):
        hits = 0
        top_1_hits = 0
        reciprocal_ranks = []
        for judgment in self.fixture["queries"]:
            result = self.workspace.search({"query": judgment["query"], "limit": 5})
            subjects = [item["subject_key"] for item in result["results"]]
            if judgment["expected"] in subjects:
                hits += 1
                rank = subjects.index(judgment["expected"]) + 1
                top_1_hits += rank == 1
                reciprocal_ranks.append(1.0 / rank)
            else:
                reciprocal_ranks.append(0.0)

        recall_at_5 = hits / len(self.fixture["queries"])
        top_1_accuracy = top_1_hits / len(self.fixture["queries"])
        mean_reciprocal_rank = sum(reciprocal_ranks) / len(reciprocal_ranks)
        self.assertGreaterEqual(recall_at_5, 0.95)
        self.assertGreaterEqual(top_1_accuracy, 0.85)
        self.assertGreaterEqual(mean_reciprocal_rank, 0.85)

    def test_spatial_radius_and_region_boundaries_are_exact(self):
        near_home = self.workspace.search(
            {
                "dimension": "overworld",
                "center": [10, 64, 20],
                "radius": 1,
                "limit": 20,
            }
        )
        self.assertEqual(
            {item["subject_key"] for item in near_home["results"]},
            {"place:home"},
        )

        village_edge = self.workspace.search(
            {
                "dimension": "overworld",
                "region": [124, 70, 24, 124, 70, 24],
                "limit": 20,
            }
        )
        self.assertEqual(
            {item["subject_key"] for item in village_edge["results"]},
            {"place:east-village"},
        )

    def test_results_explain_retrieval_lanes_and_are_bounded(self):
        result = self.workspace.search({"query": "portal", "limit": 1})
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0]["match_lanes"])
        self.assertIn("content_truncated", result["results"][0])
        self.assertIn("lanes", result)


class MemoryToolTests(unittest.TestCase):
    def test_tools_expose_agentic_crud_without_body_mutation(self):
        store = RuntimeStateStore(":memory:")
        workspace = MemoryWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        registry = ToolRegistry()
        register_memory_tools(registry, workspace)

        rejected = registry.get("write_memory").callable(
            {
                "kind": "episodic",
                "source": "observed",
                "title": "Unproven observation",
                "content": "No evidence handle was supplied.",
            }
        )
        written = registry.get("write_memory").callable(
            {
                "kind": "episodic",
                "source": "player_told",
                "title": "Player preference",
                "content": "Keep the west gate closed.",
                "subject_key": "player:west-gate",
                "evidence_ref": "conversation:2",
            }
        )
        searched = registry.get("search_memory").callable({"query": "west gate"})
        read = registry.get("read_memory").callable(
            {"memory_id": written.metrics["memory_id"]}
        )

        self.assertFalse(rejected.success)
        self.assertEqual(rejected.reason, "memory_write_rejected")
        self.assertTrue(written.success)
        self.assertTrue(searched.success)
        self.assertTrue(read.success)
        self.assertEqual(
            set(registry.names()),
            {
                "search_memory",
                "read_memory",
                "write_memory",
                "update_memory",
                "delete_memory",
            },
        )
        self.assertTrue(all(not registry.sidecar(name).mutating for name in registry.names()))
        store.close()


if __name__ == "__main__":
    unittest.main()
