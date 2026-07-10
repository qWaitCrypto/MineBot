"""Provider-portable, bounded conversation storage for openai-agents runs."""

from __future__ import annotations

import copy
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents import SQLiteSession, SessionSettings

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore

CONVERSATION_WINDOW_TURNS = 12


class WindowedConversationSession:
    """SDK Session that keeps complete recent turns without unbounded growth."""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        max_turns: int = CONVERSATION_WINDOW_TURNS,
    ) -> None:
        self.session_id = session_id or f"minebot-{uuid4()}"
        self.session_settings: SessionSettings | None = None
        self.max_turns = max(1, int(max_turns))
        self._items: list[Any] = []
        self._lock = threading.RLock()
        self._closed = False

    async def get_items(self, limit: int | None = None) -> list[Any]:
        with self._lock:
            self._require_open()
            if limit is None:
                items = self._items
            elif limit <= 0:
                items = []
            else:
                items = _complete_turn_item_limit(self._items, limit)
            return copy.deepcopy(items)

    async def add_items(self, items: list[Any]) -> None:
        if not items:
            return
        with self._lock:
            self._require_open()
            self._items = bounded_session_input(
                self._items,
                copy.deepcopy(items),
                max_turns=self.max_turns,
            )

    async def pop_item(self) -> Any | None:
        with self._lock:
            self._require_open()
            if not self._items:
                return None
            return copy.deepcopy(self._items.pop())

    async def clear_session(self) -> None:
        with self._lock:
            self._require_open()
            self._items.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._items.clear()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("conversation session is closed")


class PersistentWindowedConversationSession:
    """File-backed SDK Session with complete-turn bounded model retrieval."""

    def __init__(
        self,
        session_id: str,
        db_path: str | Path,
        *,
        max_turns: int = CONVERSATION_WINDOW_TURNS,
        archive_store: RuntimeStateStore | None = None,
        scope: RuntimeScope | None = None,
    ) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.max_turns = max(1, int(max_turns))
        self._session = SQLiteSession(session_id=session_id, db_path=db_path)
        self.session_settings = self._session.session_settings
        if (archive_store is None) != (scope is None):
            raise ValueError("archive_store and scope must be provided together")
        self.archive_store = archive_store
        self.scope = scope

    async def get_items(self, limit: int | None = None) -> list[Any]:
        archived = await self._session.get_items(limit=None)
        self._sync_archive(archived)
        items = bounded_session_input([], archived, max_turns=self.max_turns)
        if limit is None:
            return items
        if limit <= 0:
            return []
        return _complete_turn_item_limit(items, limit)

    async def sync_archive(self) -> None:
        self._sync_archive(await self._session.get_items(limit=None))

    async def add_items(self, items: list[Any]) -> None:
        await self._session.add_items(items)
        self._sync_archive(await self._session.get_items(limit=None))

    async def pop_item(self) -> Any | None:
        item = await self._session.pop_item()
        self._sync_archive(await self._session.get_items(limit=None))
        return item

    async def clear_session(self) -> None:
        await self._session.clear_session()
        if self.archive_store is not None and self.scope is not None:
            self.archive_store.clear_conversation_archive(self.scope)

    def close(self) -> None:
        self._session.close()

    def summary_payload(self) -> dict[str, object] | None:
        archive = self._archive()
        if archive is None:
            return None
        return {
            **dict(archive["summary"]),
            "archive_revision": archive["revision"],
            "archive_item_count": archive["item_count"],
        }

    def query_archive(
        self,
        *,
        query: str = "",
        start: int = 0,
        limit: int = 5,
    ) -> dict[str, object]:
        archive = self._archive()
        turns = [] if archive is None else _closed_turns(archive["items"], self.scope)
        needle = query.strip().casefold()
        if needle:
            turns = [
                turn
                for turn in turns
                if needle in json.dumps(turn["items"], ensure_ascii=False).casefold()
            ]
        start = max(0, int(start))
        limit = max(1, min(20, int(limit)))
        page = turns[start : start + limit]
        next_start = start + len(page) if start + len(page) < len(turns) else None
        return {
            "query": query,
            "start": start,
            "limit": limit,
            "total_matches": len(turns),
            "results": [_turn_summary(turn) for turn in page],
            "next_start": next_start,
            "complete": next_start is None,
        }

    def read_archive_turn(
        self,
        handle: str,
        *,
        start: int = 0,
        limit: int = 20,
    ) -> dict[str, object] | None:
        archive = self._archive()
        turns = [] if archive is None else _closed_turns(archive["items"], self.scope)
        turn = next((candidate for candidate in turns if candidate["handle"] == handle), None)
        if turn is None:
            return None
        start = max(0, int(start))
        limit = max(1, min(50, int(limit)))
        items = turn["items"]
        page = items[start : start + limit]
        next_start = start + len(page) if start + len(page) < len(items) else None
        return {
            "handle": handle,
            "turn": turn["ordinal"],
            "start": start,
            "limit": limit,
            "item_count": len(items),
            "items": copy.deepcopy(page),
            "next_start": next_start,
            "complete": next_start is None,
        }

    def _sync_archive(self, items: list[Any]) -> None:
        if self.archive_store is None or self.scope is None:
            return
        normalized = [_json_item(item) for item in items]
        summary = _conversation_summary(normalized, self.scope, max_turns=self.max_turns)
        current = self.archive_store.get_conversation_archive(self.scope)
        if (
            current is not None
            and current["items"] == normalized
            and current["summary"] == summary
        ):
            return
        self.archive_store.replace_conversation_archive(
            self.scope,
            items=normalized,
            summary=summary,
        )

    def _archive(self) -> dict[str, object] | None:
        if self.archive_store is None or self.scope is None:
            return None
        return self.archive_store.get_conversation_archive(self.scope)


def bounded_session_input(
    history: list[Any],
    new_items: list[Any],
    *,
    max_turns: int = CONVERSATION_WINDOW_TURNS,
) -> list[Any]:
    """Keep complete recent outer turns, including tool call/result pairs."""
    combined = [*history, *new_items]
    user_turn_starts = [
        index
        for index, item in enumerate(combined)
        if _session_item_role(item) == "user"
    ]
    if len(user_turn_starts) <= max_turns:
        return combined
    return combined[user_turn_starts[-max_turns] :]


def _complete_turn_item_limit(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0 or not items:
        return []
    starts = [index for index, item in enumerate(items) if _session_item_role(item) == "user"]
    if not starts:
        return list(items) if len(items) <= limit else []
    turns = [
        items[start : starts[index + 1] if index + 1 < len(starts) else len(items)]
        for index, start in enumerate(starts)
    ]
    selected: list[list[Any]] = []
    item_count = 0
    for turn in reversed(turns):
        if selected and item_count + len(turn) > limit:
            break
        selected.append(turn)
        item_count += len(turn)
        if item_count >= limit:
            break
    return [item for turn in reversed(selected) for item in turn]


def _session_item_role(item: Any) -> object:
    if isinstance(item, dict):
        return item.get("role")
    return getattr(item, "role", None)


def _json_item(item: Any) -> object:
    if isinstance(item, dict):
        return copy.deepcopy(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return json.loads(json.dumps(item, ensure_ascii=False, default=str))


def _closed_turns(items: list[object], scope: RuntimeScope | None) -> list[dict[str, object]]:
    starts = [index for index, item in enumerate(items) if _session_item_role(item) == "user"]
    turns: list[dict[str, object]] = []
    scope_key = "memory" if scope is None else scope.key
    for ordinal, start in enumerate(starts, start=1):
        end = starts[ordinal] if ordinal < len(starts) else len(items)
        turn_items = items[start:end]
        call_ids = [
            str(item.get("call_id") or item.get("id") or "")
            for item in turn_items
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        output_ids = [
            str(item.get("call_id") or "")
            for item in turn_items
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        protocol_indices = [
            index
            for index, item in enumerate(turn_items)
            if isinstance(item, dict)
            and item.get("type") in {"function_call", "function_call_output"}
        ]
        assistant_indices = [
            index
            for index, item in enumerate(turn_items)
            if isinstance(item, dict) and _session_item_role(item) == "assistant"
        ]
        protocol_closed = (
            all(call_ids)
            and all(output_ids)
            and Counter(call_ids) == Counter(output_ids)
        ) if (call_ids or output_ids) else True
        last_protocol_index = max(protocol_indices, default=0)
        has_final_response = any(index > last_protocol_index for index in assistant_indices)
        if not protocol_closed or not has_final_response:
            continue
        turns.append(
            {
                "handle": f"conversation:{scope_key}:turn:{ordinal}",
                "ordinal": ordinal,
                "items": copy.deepcopy(turn_items),
            }
        )
    return turns


def _conversation_summary(
    items: list[object],
    scope: RuntimeScope,
    *,
    max_turns: int,
) -> dict[str, object]:
    turns = _closed_turns(items, scope)
    compacted = turns[:-max_turns] if len(turns) > max_turns else []
    live_items = bounded_session_input([], items, max_turns=max_turns)
    return {
        "scope_key": scope.key,
        "total_closed_turns": len(turns),
        "live_turns": min(len(turns), max_turns),
        "compacted_turns": len(compacted),
        "covered_turn_handles": [turn["handle"] for turn in compacted],
        "recent_compacted": [_turn_summary(turn) for turn in compacted[-20:]],
        "live_item_count": len(live_items),
        "live_item_chars": len(json.dumps(live_items, ensure_ascii=False, sort_keys=True)),
        "archive_item_chars": len(json.dumps(items, ensure_ascii=False, sort_keys=True)),
        "complete": True,
    }


def _turn_summary(turn: dict[str, object]) -> dict[str, object]:
    items = turn["items"]
    user_text = ""
    assistant_text = ""
    tools: list[str] = []
    reasons: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = _session_item_role(item)
        if role == "user" and not user_text:
            user_text = _item_text(item)
        elif role == "assistant":
            assistant_text = _item_text(item) or assistant_text
        if item.get("type") == "function_call":
            name = str(item.get("name") or "")
            if name:
                tools.append(name)
        if item.get("type") == "function_call_output":
            output = item.get("output")
            try:
                decoded = json.loads(output) if isinstance(output, str) else output
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict) and decoded.get("reason"):
                reasons.append(str(decoded["reason"]))
    return {
        "handle": turn["handle"],
        "turn": turn["ordinal"],
        "user": user_text[:1000],
        "assistant": assistant_text[:1000],
        "tools": tools[:32],
        "tool_reasons": reasons[:32],
        "item_count": len(items),
    }


def _item_text(item: dict[str, object]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for value in content:
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict):
                text = value.get("text") or value.get("content")
                if text:
                    parts.append(str(text))
        return " ".join(parts)
    return ""


__all__ = [
    "CONVERSATION_WINDOW_TURNS",
    "PersistentWindowedConversationSession",
    "WindowedConversationSession",
    "bounded_session_input",
]
