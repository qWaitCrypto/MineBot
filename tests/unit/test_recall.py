"""F3 recall: unified fan-out, bounded projections, evidence-handle discipline.

brain-cognitive-framework.md §6. Recall must return projections with
resolvable handles (never full payloads), record an unavailable source
instead of silently returning fewer results, and interleave sources fairly
rather than pretending cross-store scores are comparable.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from minebot.app.recall import (
    ConversationRecallSource,
    FanOutRecall,
    MemoryRecallSource,
    ObservationRecallSource,
    SkillRecallSource,
    WikiRecallSource,
)
from minebot.app.runtime_state import (
    MemoryKind,
    MemorySource,
    MemoryStateConflict,
    RuntimeScope,
    RuntimeStateStore,
    memory_record_payload,
)
from minebot.brain.recall import (
    DEFAULT_RECALL_KINDS,
    EXTERNAL_RECALL_KINDS,
    RecallItem,
    RecallKind,
    RecallQuery,
    merge_recall_items,
)


class _FakeMemoryWorkspace:
    def search(self, params):
        return {
            "results": [
                {
                    "memory_id": "memory-1",
                    "title": "Spruce grove",
                    "kind": "spatial",
                    "source": "observed",
                    "excerpt": "logs at (12, 70, -8)",
                    "retrieval_score": 0.8,
                }
            ]
        }


class _FakeConversationArchive:
    def query_archive(self, *, query="", start=0, limit=5):
        return {"results": [{"handle": "turn:7", "turn": 7, "user": "hi", "assistant": "hey"}]}


class _FakeObservationArchive:
    def query(self, *, query="", tool_name="", reason="", start=0, limit=10):
        return {
            "results": [
                {"handle": "observation:9", "tool": "read_state", "success": True, "reason": "state_read"}
            ]
        }


class _FakeSkillWorkspace:
    def list(self, query="", *, start=0, limit=20):
        return {
            "skills": [
                {"name": "resource-progression", "version": "3", "description": "how to climb the ladder"}
            ]
        }


class _FakeWiki:
    def search(self, query, *, limit=5):
        return {"results": [{"title": "Spruce", "snippet": "a tree"}]}


class _BrokenSource:
    kind = RecallKind.MEMORY

    def search(self, text, *, limit):
        raise RuntimeError("store offline")


def _full_recall() -> FanOutRecall:
    return FanOutRecall(
        [
            MemoryRecallSource(_FakeMemoryWorkspace()),
            ConversationRecallSource(_FakeConversationArchive()),
            ObservationRecallSource(_FakeObservationArchive()),
            SkillRecallSource(_FakeSkillWorkspace()),
            WikiRecallSource(_FakeWiki()),
        ]
    )


class FanOutTests(unittest.TestCase):
    def test_default_query_excludes_external_sources(self) -> None:
        self.assertEqual(EXTERNAL_RECALL_KINDS, frozenset({RecallKind.WIKI}))
        self.assertNotIn(RecallKind.WIKI, DEFAULT_RECALL_KINDS)

        result = _full_recall().recall(RecallQuery(text="spruce"))
        self.assertEqual(result.by_kind(RecallKind.WIKI), ())
        self.assertEqual(result.unavailable, ())

    def test_wiki_is_reachable_when_explicitly_requested(self) -> None:
        result = _full_recall().recall(
            RecallQuery(text="spruce", kinds=frozenset({RecallKind.WIKI}))
        )
        self.assertEqual(len(result.by_kind(RecallKind.WIKI)), 1)
        self.assertEqual(result.items[0].handle, "wiki:Spruce")

    def test_every_item_carries_a_resolvable_handle_and_provenance(self) -> None:
        result = _full_recall().recall(RecallQuery(text="spruce"))
        self.assertTrue(result.items)
        for item in result.items:
            self.assertTrue(item.handle, item)
            self.assertTrue(item.provenance, item)
            self.assertIsInstance(item.projection, dict)

    def test_projections_stay_bounded_rather_than_returning_full_payloads(self) -> None:
        class _LongMemory:
            def search(self, params):
                return {
                    "results": [
                        {
                            "memory_id": "memory-long",
                            "title": "t",
                            "excerpt": "x" * 5000,
                            "source": "observed",
                        }
                    ]
                }

        recall = FanOutRecall([MemoryRecallSource(_LongMemory())])
        item = recall.recall(RecallQuery(text="x", kinds=frozenset({RecallKind.MEMORY}))).items[0]
        self.assertLessEqual(len(str(item.projection["excerpt"])), 300)

    def test_missing_source_is_reported_unavailable_not_silently_empty(self) -> None:
        recall = FanOutRecall([MemoryRecallSource(_FakeMemoryWorkspace())])
        result = recall.recall(
            RecallQuery(text="x", kinds=frozenset({RecallKind.MEMORY, RecallKind.SKILL}))
        )
        self.assertEqual(result.unavailable, ("skill",))
        self.assertEqual(len(result.items), 1)

    def test_failing_source_is_recorded_with_its_error(self) -> None:
        seen: list[tuple[str, str]] = []
        recall = FanOutRecall(
            [_BrokenSource()], on_error=lambda kind, exc: seen.append((kind, type(exc).__name__))
        )
        result = recall.recall(RecallQuery(text="x", kinds=frozenset({RecallKind.MEMORY})))

        self.assertEqual(result.items, ())
        self.assertEqual(result.unavailable, ("memory",))
        self.assertEqual(result.errors, ("memory: RuntimeError",))
        self.assertEqual(seen, [("memory", "RuntimeError")])

    def test_limit_is_bounded_and_truncation_is_reported(self) -> None:
        result = _full_recall().recall(RecallQuery(text="spruce", limit=2))
        self.assertEqual(len(result.items), 2)
        self.assertTrue(result.truncated)


class MergeTests(unittest.TestCase):
    def _item(self, kind: RecallKind, handle: str) -> RecallItem:
        return RecallItem(kind=kind, handle=handle, projection={}, provenance="p")

    def test_round_robin_keeps_every_source_represented(self) -> None:
        groups = (
            (
                self._item(RecallKind.MEMORY, "m1"),
                self._item(RecallKind.MEMORY, "m2"),
                self._item(RecallKind.MEMORY, "m3"),
            ),
            (self._item(RecallKind.SKILL, "s1"),),
        )
        merged, truncated = merge_recall_items(groups, limit=2)
        self.assertEqual([item.handle for item in merged], ["m1", "s1"])
        self.assertTrue(truncated)

    def test_no_truncation_flag_when_everything_fits(self) -> None:
        groups = ((self._item(RecallKind.MEMORY, "m1"),),)
        merged, truncated = merge_recall_items(groups, limit=8)
        self.assertEqual(len(merged), 1)
        self.assertFalse(truncated)


class MemoryEvidenceFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStateStore(Path(self._dir.name) / "state.db")
        self.scope = RuntimeScope("server", "world", "bot")
        self.store.register_scope(self.scope)

    def tearDown(self) -> None:
        self.store.close()
        self._dir.cleanup()

    def _write(self, title: str, handles=()) -> object:
        return self.store.create_memory(
            self.scope,
            kind=MemoryKind.EPISODIC,
            source=MemorySource.OBSERVED,
            title=title,
            content="body",
            evidence_ref="observation:1",
            evidence_handles=handles,
        )

    def test_handles_round_trip_and_reach_the_payload(self) -> None:
        record = self._write("grove", handles=["observation:1", "turn:4"])
        self.assertEqual(record.evidence_handles, ("observation:1", "turn:4"))
        payload = memory_record_payload(record)
        self.assertEqual(payload["evidence_handles"], ["observation:1", "turn:4"])
        self.assertIsNone(payload["superseded_by"])

    def test_handles_are_deduplicated_and_bounded(self) -> None:
        record = self._write("grove", handles=["a", "a", " ", "b"] + [f"h{i}" for i in range(30)])
        self.assertEqual(record.evidence_handles[:2], ("a", "b"))
        self.assertLessEqual(len(record.evidence_handles), 16)

    def test_default_is_empty_so_existing_writes_are_unaffected(self) -> None:
        self.assertEqual(self._write("plain").evidence_handles, ())

    def test_supersession_chains_instead_of_deleting(self) -> None:
        old = self._write("old fact")
        new = self._write("new fact")

        retired = self.store.supersede_memory(
            self.scope,
            old.memory_id,
            superseded_by=new.memory_id,
            expected_revision=old.revision,
        )
        self.assertEqual(retired.superseded_by, new.memory_id)
        # The retired fact stays resolvable, which is the point.
        self.assertIsNotNone(self.store.get_memory(self.scope, old.memory_id))

    def test_supersession_rejects_self_reference_and_unknown_replacements(self) -> None:
        record = self._write("fact")
        with self.assertRaises(MemoryStateConflict):
            self.store.supersede_memory(
                self.scope,
                record.memory_id,
                superseded_by=record.memory_id,
                expected_revision=record.revision,
            )
        with self.assertRaises(MemoryStateConflict):
            self.store.supersede_memory(
                self.scope,
                record.memory_id,
                superseded_by="memory-ghost",
                expected_revision=record.revision,
            )

    def test_supersession_honors_optimistic_revisions(self) -> None:
        old = self._write("old")
        new = self._write("new")
        with self.assertRaises(MemoryStateConflict):
            self.store.supersede_memory(
                self.scope,
                old.memory_id,
                superseded_by=new.memory_id,
                expected_revision=old.revision + 5,
            )


if __name__ == "__main__":
    unittest.main()
