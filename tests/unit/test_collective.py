"""F8 collective: transfer idempotency, one-shot resolution, team-fact ownership.

brain-cognitive-framework.md §11. Two invariants dominate: conversation
history never transfers, and a team-fact key has exactly one writer. Both are
what keep one-brain-per-bot process isolation (C8) intact.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from minebot.app.collective_store import InMemoryTaskTransferStore, InMemoryTeamFactStore
from minebot.brain.collective import (
    MAX_BOUNDED_FACTS_CHARS,
    CollectiveError,
    TaskTransferRecord,
    TeamFact,
    TransferStatus,
    validate_transfer,
)


def _transfer(
    transfer_id: str = "t-1",
    *,
    idempotency_key: str = "key-1",
    to_bot: str = "Bot2",
    **kwargs,
) -> TaskTransferRecord:
    return TaskTransferRecord(
        transfer_id=transfer_id,
        idempotency_key=idempotency_key,
        from_bot="Bot1",
        to_bot=to_bot,
        task_snapshot={"task_id": "task-9", "goal": "collect 64 logs"},
        **{"bounded_facts": {"last_position": [12, 70, -8]}, **kwargs},
    )


class TransferValidationTests(unittest.TestCase):
    def test_valid_transfer_round_trips(self) -> None:
        record = validate_transfer(_transfer())
        self.assertIs(record.status, TransferStatus.OFFERED)
        self.assertFalse(record.resolved)

    def test_conversation_history_may_never_transfer(self) -> None:
        for key in ("conversation", "messages", "transcript", "history", "items"):
            with self.assertRaises(CollectiveError, msg=key):
                validate_transfer(
                    TaskTransferRecord(
                        transfer_id="t",
                        idempotency_key="k",
                        from_bot="Bot1",
                        to_bot="Bot2",
                        task_snapshot={key: ["turn 1", "turn 2"]},
                    )
                )

    def test_bounded_facts_are_size_capped(self) -> None:
        with self.assertRaises(CollectiveError):
            validate_transfer(
                _transfer(bounded_facts={"blob": "x" * (MAX_BOUNDED_FACTS_CHARS + 1)})
            )

    def test_self_transfer_and_missing_identity_are_rejected(self) -> None:
        with self.assertRaises(CollectiveError):
            validate_transfer(_transfer(to_bot="Bot1"))
        with self.assertRaises(CollectiveError):
            validate_transfer(_transfer(idempotency_key=" "))


class TransferResolutionTests(unittest.TestCase):
    def test_offer_is_idempotent_under_retry(self) -> None:
        store = InMemoryTaskTransferStore()
        first = store.offer(_transfer("t-1", idempotency_key="same"))
        second = store.offer(_transfer("t-2", idempotency_key="same"))

        self.assertEqual(first.transfer_id, second.transfer_id)
        self.assertIsNone(store.get("t-2"))

    def test_inbox_lists_only_open_offers_for_that_bot(self) -> None:
        store = InMemoryTaskTransferStore()
        store.offer(_transfer("t-1", idempotency_key="a", to_bot="Bot2"))
        store.offer(_transfer("t-2", idempotency_key="b", to_bot="Bot3"))

        self.assertEqual([item.transfer_id for item in store.inbox("Bot2")], ["t-1"])

        store.resolve("t-1", TransferStatus.ACCEPTED)
        self.assertEqual(store.inbox("Bot2"), ())

    def test_a_transfer_resolves_exactly_once(self) -> None:
        store = InMemoryTaskTransferStore()
        store.offer(_transfer())
        store.resolve("t-1", TransferStatus.REJECTED)

        # Re-resolving would let a rejected task be quietly accepted later.
        with self.assertRaises(CollectiveError):
            store.resolve("t-1", TransferStatus.ACCEPTED)

    def test_resolving_an_unknown_transfer_is_an_error(self) -> None:
        with self.assertRaises(CollectiveError):
            InMemoryTaskTransferStore().resolve("ghost", TransferStatus.ACCEPTED)

    def test_campaign_reference_is_carried_for_delegation(self) -> None:
        record = validate_transfer(_transfer(campaign_ref=("dragon", "iron")))
        self.assertEqual(record.campaign_ref, ("dragon", "iron"))


class TeamFactTests(unittest.TestCase):
    def test_single_writer_per_key_is_enforced(self) -> None:
        store = InMemoryTeamFactStore()
        store.put(TeamFact(key="base.location", value={"pos": [0, 64, 0]}, writer_bot="Bot1"))

        with self.assertRaises(CollectiveError):
            store.put(TeamFact(key="base.location", value={"pos": [9, 9, 9]}, writer_bot="Bot2"))

    def test_owner_updates_bump_the_sequence(self) -> None:
        store = InMemoryTeamFactStore()
        first = store.put(TeamFact(key="k", value={"v": 1}, writer_bot="Bot1"))
        second = store.put(TeamFact(key="k", value={"v": 2}, writer_bot="Bot1"))

        self.assertEqual(first.seq, 0)
        self.assertEqual(second.seq, 1)
        self.assertEqual(store.get("k").value, {"v": 2})

    def test_provenance_is_required(self) -> None:
        store = InMemoryTeamFactStore()
        with self.assertRaises(CollectiveError):
            store.put(TeamFact(key="k", value={}, writer_bot=" "))

    def test_facts_are_size_capped(self) -> None:
        store = InMemoryTeamFactStore()
        with self.assertRaises(CollectiveError):
            store.put(TeamFact(key="k", value={"blob": "x" * 5000}, writer_bot="Bot1"))


class HandoffRejectionGuardTests(unittest.TestCase):
    """Multi-bot must not regress to SDK handoffs (decision 2026-07-26)."""

    def test_no_module_imports_sdk_handoffs(self) -> None:
        offenders: list[str] = []
        for path in sorted(Path("minebot").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "agents":
                    offenders.extend(
                        f"{path}:{alias.name}"
                        for alias in node.names
                        if alias.name in {"handoff", "Handoff"}
                    )
        self.assertEqual(
            offenders,
            [],
            "SDK handoffs transfer full conversation history in-process, which "
            "is incompatible with one brain per bot; use TaskTransfer artifacts.",
        )


if __name__ == "__main__":
    unittest.main()
