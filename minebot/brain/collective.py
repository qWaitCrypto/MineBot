"""F8 Collective — task transfer and team facts for multi-bot MineBot.

brain-cognitive-framework.md §11. Recorded correction: the earlier
extension-slot sketch mapped multi-bot onto SDK handoffs. Handoffs are an
in-process agent swap that transfers full conversation history, which is
architecturally wrong for one-brain-per-bot process isolation (C8) and was
already flagged as a cost concern. The mechanism is **task exchange through
the durable store**.

Two invariants the types enforce by construction:

- **Conversation history never transfers.** A transfer carries a task
  snapshot and explicitly bounded facts. Two brains sharing a transcript
  would make them one brain with extra steps.
- **Team facts have exactly one writer per key.** Brains stay sovereign; the
  shared surface is append-mediated facts with provenance, never shared
  mutable state (C8).

Framework-agnostic: imports only stdlib.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum

JsonObject = dict[str, object]

MAX_BOUNDED_FACTS_CHARS = 8_000
MAX_TEAM_FACT_CHARS = 4_000


class TransferStatus(str, Enum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


_TERMINAL_STATUSES = frozenset(
    {TransferStatus.ACCEPTED, TransferStatus.REJECTED, TransferStatus.EXPIRED}
)

# A transfer resolves exactly once. Re-resolving would let a rejected task be
# quietly accepted later, or an accepted task be double-promoted.
_ALLOWED_TRANSITIONS: dict[TransferStatus, frozenset[TransferStatus]] = {
    TransferStatus.OFFERED: frozenset(_TERMINAL_STATUSES),
    TransferStatus.ACCEPTED: frozenset(),
    TransferStatus.REJECTED: frozenset(),
    TransferStatus.EXPIRED: frozenset(),
}


class CollectiveError(ValueError):
    """A transfer or team-fact write would violate a collective invariant."""


@dataclass(frozen=True)
class TaskTransferRecord:
    transfer_id: str
    idempotency_key: str
    from_bot: str
    to_bot: str
    task_snapshot: JsonObject = field(default_factory=dict)
    bounded_facts: JsonObject = field(default_factory=dict)
    campaign_ref: tuple[str, str] | None = None   # (campaign_id, node_id)
    status: TransferStatus = TransferStatus.OFFERED
    created_at: str = ""
    resolved_at: str = ""

    @property
    def resolved(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def resolve(self, status: TransferStatus, *, resolved_at: str = "") -> "TaskTransferRecord":
        if status not in _ALLOWED_TRANSITIONS[self.status]:
            raise CollectiveError(
                f"transfer {self.transfer_id}: {self.status.value} -> {status.value} is not allowed"
            )
        return replace(self, status=status, resolved_at=resolved_at)


# Keys a task snapshot may never carry. A transfer moves artifacts, not a
# transcript; this is the structural half of "conversation never transfers".
_FORBIDDEN_SNAPSHOT_KEYS = frozenset(
    {
        "conversation",
        "conversation_items",
        "history",
        "messages",
        "session_messages",
        "transcript",
        "items",
    }
)


def validate_transfer(record: TaskTransferRecord) -> TaskTransferRecord:
    if not record.transfer_id.strip():
        raise CollectiveError("transfer_id is required")
    if not record.idempotency_key.strip():
        raise CollectiveError("idempotency_key is required")
    if not record.from_bot.strip() or not record.to_bot.strip():
        raise CollectiveError("from_bot and to_bot are required")
    if record.from_bot == record.to_bot:
        raise CollectiveError("a bot cannot transfer work to itself")
    _reject_conversation_payload(record.task_snapshot, where="task_snapshot")
    _reject_conversation_payload(record.bounded_facts, where="bounded_facts")
    if _encoded_size(record.bounded_facts) > MAX_BOUNDED_FACTS_CHARS:
        raise CollectiveError(
            f"bounded_facts exceeds {MAX_BOUNDED_FACTS_CHARS} chars; "
            "send handles, not payloads"
        )
    if record.campaign_ref is not None and len(record.campaign_ref) != 2:
        raise CollectiveError("campaign_ref must be (campaign_id, node_id)")
    return record


def _reject_conversation_payload(payload: JsonObject, *, where: str) -> None:
    for key in payload:
        if str(key).strip().lower() in _FORBIDDEN_SNAPSHOT_KEYS:
            raise CollectiveError(
                f"{where} may not carry conversation history (key {key!r}); "
                "transfer task artifacts and bounded facts only"
            )


def _encoded_size(payload: JsonObject) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return len(str(payload))


@dataclass(frozen=True)
class TeamFact:
    key: str
    value: JsonObject
    writer_bot: str
    seq: int = 0
    updated_at: str = ""


def validate_team_fact(fact: TeamFact) -> TeamFact:
    if not fact.key.strip():
        raise CollectiveError("team fact key is required")
    if not fact.writer_bot.strip():
        raise CollectiveError("team fact requires a writer_bot for provenance")
    if _encoded_size(fact.value) > MAX_TEAM_FACT_CHARS:
        raise CollectiveError(f"team fact exceeds {MAX_TEAM_FACT_CHARS} chars")
    return fact


__all__ = [
    "MAX_BOUNDED_FACTS_CHARS",
    "MAX_TEAM_FACT_CHARS",
    "CollectiveError",
    "TaskTransferRecord",
    "TeamFact",
    "TransferStatus",
    "validate_team_fact",
    "validate_transfer",
]
