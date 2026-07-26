"""Fan-out recall over MineBot's concrete knowledge stores (F3).

brain-cognitive-framework.md §6. Each adapter translates one existing store's
query surface into bounded `RecallItem`s; `FanOutRecall` composes whichever
adapters are wired. Nothing here changes the stores or the agent-visible tool
schemas — the existing memory/archive/skill/wiki tools keep their exact
contracts, and this is the shared path that future retrieval work (semantic
backends, team scope, distillation evidence lookups) plugs into instead of
growing a private query path.

A source that raises is recorded as unavailable, never silently dropped: an
empty recall and a broken store must not look identical to a caller.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from minebot.app.projection import bounded_summary_value, shorten
from minebot.brain.recall import (
    JsonObject,
    RecallItem,
    RecallKind,
    RecallQuery,
    RecallResult,
    RecallSource,
    merge_recall_items,
)


def _rows(payload: object, key: str = "results") -> Sequence[JsonObject]:
    if isinstance(payload, dict):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return ()


class MemoryRecallSource:
    kind = RecallKind.MEMORY

    def __init__(self, workspace: object) -> None:
        self._workspace = workspace

    def search(self, text: str, *, limit: int) -> tuple[RecallItem, ...]:
        payload = self._workspace.search({"query": text, "limit": limit})  # type: ignore[attr-defined]
        items: list[RecallItem] = []
        for row in _rows(payload):
            memory_id = str(row.get("memory_id") or "")
            if not memory_id:
                continue
            items.append(
                RecallItem(
                    kind=self.kind,
                    handle=f"memory:{memory_id}",
                    projection={
                        "title": bounded_summary_value(row.get("title")),
                        "kind": row.get("kind"),
                        "subject_key": row.get("subject_key"),
                        "excerpt": shorten(str(row.get("excerpt") or ""), limit=300),
                    },
                    provenance=str(row.get("source") or "memory"),
                    score=float(row.get("retrieval_score") or 0.0),
                )
            )
        return tuple(items)


class ConversationRecallSource:
    kind = RecallKind.CONVERSATION

    def __init__(self, archive: object) -> None:
        self._archive = archive

    def search(self, text: str, *, limit: int) -> tuple[RecallItem, ...]:
        payload = self._archive.query_archive(query=text, limit=limit)  # type: ignore[attr-defined]
        items: list[RecallItem] = []
        for row in _rows(payload):
            handle = str(row.get("handle") or "")
            if not handle:
                continue
            items.append(
                RecallItem(
                    kind=self.kind,
                    handle=handle,
                    projection={
                        "turn": row.get("turn"),
                        "user": shorten(str(row.get("user") or ""), limit=200),
                        "assistant": shorten(str(row.get("assistant") or ""), limit=200),
                        "tools": bounded_summary_value(row.get("tools")),
                    },
                    provenance="conversation_archive",
                )
            )
        return tuple(items)


class ObservationRecallSource:
    kind = RecallKind.OBSERVATION

    def __init__(self, archive: object) -> None:
        self._archive = archive

    def search(self, text: str, *, limit: int) -> tuple[RecallItem, ...]:
        payload = self._archive.query(query=text, limit=limit)  # type: ignore[attr-defined]
        items: list[RecallItem] = []
        for row in _rows(payload):
            handle = str(row.get("handle") or "")
            if not handle:
                continue
            items.append(
                RecallItem(
                    kind=self.kind,
                    handle=handle,
                    projection={
                        "tool": row.get("tool"),
                        "success": row.get("success"),
                        "reason": row.get("reason"),
                        "created_at": row.get("created_at"),
                    },
                    provenance="tool_observation",
                )
            )
        return tuple(items)


class SkillRecallSource:
    kind = RecallKind.SKILL

    def __init__(self, workspace: object) -> None:
        self._workspace = workspace

    def search(self, text: str, *, limit: int) -> tuple[RecallItem, ...]:
        payload = self._workspace.list(text, limit=limit)  # type: ignore[attr-defined]
        items: list[RecallItem] = []
        for row in _rows(payload, key="skills") or _rows(payload):
            name = str(row.get("name") or "")
            if not name:
                continue
            version = str(row.get("version") or row.get("head_version") or "")
            items.append(
                RecallItem(
                    kind=self.kind,
                    handle=f"skill:{name}@{version}" if version else f"skill:{name}",
                    projection={
                        "name": name,
                        "version": version,
                        "description": shorten(str(row.get("description") or ""), limit=300),
                        "loadable": row.get("loadable"),
                    },
                    provenance=str(row.get("origin") or "skill"),
                )
            )
        return tuple(items)


class WikiRecallSource:
    """Advisory external prose; excluded from the default fan-out."""

    kind = RecallKind.WIKI

    def __init__(self, knowledge: object) -> None:
        self._knowledge = knowledge

    def search(self, text: str, *, limit: int) -> tuple[RecallItem, ...]:
        if not text.strip():
            return ()
        payload = self._knowledge.search(text, limit=limit)  # type: ignore[attr-defined]
        items: list[RecallItem] = []
        for row in _rows(payload):
            title = str(row.get("title") or "")
            if not title:
                continue
            items.append(
                RecallItem(
                    kind=self.kind,
                    handle=f"wiki:{title}",
                    projection={
                        "title": title,
                        "snippet": shorten(str(row.get("snippet") or ""), limit=300),
                    },
                    provenance="minecraft.wiki",
                )
            )
        return tuple(items)


class FanOutRecall:
    """Compose the wired sources into one bounded, fairly-interleaved result."""

    def __init__(
        self,
        sources: Sequence[RecallSource],
        *,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self._sources = {source.kind: source for source in sources}
        self._on_error = on_error

    @property
    def kinds(self) -> frozenset[RecallKind]:
        return frozenset(self._sources)

    def recall(self, query: RecallQuery) -> RecallResult:
        groups: list[tuple[RecallItem, ...]] = []
        unavailable: list[str] = []
        errors: list[str] = []
        per_kind = query.bounded_per_kind_limit()

        for kind in sorted(query.kinds, key=lambda item: item.value):
            source = self._sources.get(kind)
            if source is None:
                unavailable.append(kind.value)
                continue
            try:
                groups.append(source.search(query.text, limit=per_kind))
            except Exception as exc:
                unavailable.append(kind.value)
                errors.append(f"{kind.value}: {type(exc).__name__}")
                if self._on_error is not None:
                    self._on_error(kind.value, exc)

        items, truncated = merge_recall_items(tuple(groups), limit=query.bounded_limit())
        return RecallResult(
            items=items,
            truncated=truncated,
            unavailable=tuple(unavailable),
            errors=tuple(errors),
        )


__all__ = [
    "ConversationRecallSource",
    "FanOutRecall",
    "MemoryRecallSource",
    "ObservationRecallSource",
    "SkillRecallSource",
    "WikiRecallSource",
]
