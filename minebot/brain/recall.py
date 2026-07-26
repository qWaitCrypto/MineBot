"""F3 Memory System — one recall interface over all persistent knowledge.

brain-cognitive-framework.md §6. Five stores already answer "what do I know"
(explicit memory, conversation archive, observation archive, Skills, advisory
wiki) through five different query surfaces. Every long-term feature —
semantic retrieval, team memory, reflection distillation — is a "remember
things" feature, and without one interface each would grow a private query
path.

This module owns the *contract*, not the storage: query/result types, the
bounded-projection rule, and the evidence-handle discipline. `app/recall.py`
supplies the fan-out over the concrete stores.

Two rules the types enforce by construction:

- **Recall returns projections, never full payloads.** Every item carries a
  resolvable ``handle`` so the model can fetch the complete artifact through
  the existing archive tools when it actually needs it.
- **Provenance is source-as-trust, not a confidence score.** ``provenance``
  names where a fact came from; it never asserts how true it is
  (`memory-and-knowledge.md`).

Framework-agnostic: imports only stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

JsonObject = dict[str, object]


class RecallKind(str, Enum):
    MEMORY = "memory"
    CONVERSATION = "conversation"
    OBSERVATION = "observation"
    SKILL = "skill"
    WIKI = "wiki"


ALL_RECALL_KINDS = frozenset(RecallKind)

# Kinds that reach outside the server for advisory prose. They are excluded
# from the default fan-out because a recall on the hot path must never block
# on a network call; asking for them is an explicit choice.
EXTERNAL_RECALL_KINDS = frozenset({RecallKind.WIKI})

DEFAULT_RECALL_KINDS = ALL_RECALL_KINDS - EXTERNAL_RECALL_KINDS

DEFAULT_LIMIT = 8
MAX_LIMIT = 50


@dataclass(frozen=True)
class RecallQuery:
    text: str = ""
    kinds: frozenset[RecallKind] = DEFAULT_RECALL_KINDS
    limit: int = DEFAULT_LIMIT
    per_kind_limit: int | None = None

    def bounded_limit(self) -> int:
        return max(1, min(MAX_LIMIT, int(self.limit)))

    def bounded_per_kind_limit(self) -> int:
        if self.per_kind_limit is None:
            return self.bounded_limit()
        return max(1, min(MAX_LIMIT, int(self.per_kind_limit)))


@dataclass(frozen=True)
class RecallItem:
    kind: RecallKind
    handle: str            # resolvable through the owning archive/memory tool
    projection: JsonObject  # bounded; never the full payload
    provenance: str         # source-as-trust, never a confidence score
    score: float = 0.0


@dataclass(frozen=True)
class RecallResult:
    items: tuple[RecallItem, ...] = ()
    truncated: bool = False
    # Kinds that were asked for but could not answer (store absent, or an
    # advisory source failed). Recorded, never silently dropped: an empty
    # result and an unavailable source mean very different things.
    unavailable: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    def by_kind(self, kind: RecallKind) -> tuple[RecallItem, ...]:
        return tuple(item for item in self.items if item.kind is kind)

    def handles(self) -> tuple[str, ...]:
        return tuple(item.handle for item in self.items)


class Recall(Protocol):
    def recall(self, query: RecallQuery) -> RecallResult: ...


class RecallSource(Protocol):
    """One store's contribution to a recall fan-out."""

    kind: RecallKind

    def search(self, text: str, *, limit: int) -> tuple[RecallItem, ...]: ...


def merge_recall_items(
    groups: tuple[tuple[RecallItem, ...], ...],
    *,
    limit: int,
) -> tuple[tuple[RecallItem, ...], bool]:
    """Interleave per-source results fairly, then bound the total.

    Round-robin rather than global score sort: scores from different stores
    are not comparable (a lexical memory score and a wiki rank measure
    different things), so ranking across them would be a false precision.
    Fair interleaving keeps every consulted source represented.
    """

    queues = [list(group) for group in groups if group]
    merged: list[RecallItem] = []
    total = sum(len(queue) for queue in queues)
    while queues and len(merged) < limit:
        for queue in list(queues):
            if not queue:
                queues.remove(queue)
                continue
            merged.append(queue.pop(0))
            if len(merged) >= limit:
                break
    return tuple(merged), total > len(merged)


__all__ = [
    "ALL_RECALL_KINDS",
    "DEFAULT_LIMIT",
    "DEFAULT_RECALL_KINDS",
    "EXTERNAL_RECALL_KINDS",
    "MAX_LIMIT",
    "Recall",
    "RecallItem",
    "RecallKind",
    "RecallQuery",
    "RecallResult",
    "RecallSource",
    "merge_recall_items",
]
