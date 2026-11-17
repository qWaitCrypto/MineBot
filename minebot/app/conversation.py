"""Provider-portable, bounded conversation storage for openai-agents runs."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents import SQLiteSession, SessionSettings
from agents.run_config import CallModelData, ModelInputData

from minebot.app.observability import sanitize_observation
from minebot.app.prompt_cache import current_context_item
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore

CONVERSATION_WINDOW_TURNS = 12
MODEL_INPUT_MAX_ITEMS = 96
MODEL_INPUT_MAX_CHARS = 64_000


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
    """File-backed SDK Session with complete-turn bounded model retrieval.

    The SDK table holds the complete item stream; it is the archive. This
    object reads it once per process and then keeps an in-memory copy and an
    index that grow with each write. Rereading, resanitizing and rewriting the
    whole stream on every write held the event loop for 5-15 s once the
    conversation reached 11590 items (63 MB), and the Body loop, the Decision
    channel and the IPC readers stopped with it (living-continuity-20261017
    §1.2). Every write now costs what it adds.
    """

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
        self._index: _ArchiveIndex | None = None
        self._revision = 0
        self._summary: tuple[int, dict[str, object]] | None = None
        self._lock = asyncio.Lock()

    async def get_items(self, limit: int | None = None) -> list[Any]:
        async with self._lock:
            index = await self._loaded()
            items = bounded_session_input([], index.items, max_turns=self.max_turns)
        if limit is not None:
            if limit <= 0:
                return []
            items = _complete_turn_item_limit(items, limit)
        # One long autonomous turn keeps thousands of items in the window.
        # Copy them item by item off the loop so the loop keeps running.
        return await asyncio.to_thread(_copied_items, items)

    async def sync_archive(self) -> None:
        async with self._lock:
            await self._loaded()

    async def add_items(self, items: list[Any]) -> None:
        stored = [_stored_item(item) for item in items]
        async with self._lock:
            index = await self._loaded()
            await self._session.add_items(stored)
            for item in stored:
                index.append(item)
            self._revision += 1

    async def pop_item(self) -> Any | None:
        async with self._lock:
            index = await self._loaded()
            item = await self._session.pop_item()
            if item is not None and index.items:
                self._index = _ArchiveIndex(index.items[:-1])
                self._revision += 1
        return None if item is None else _sanitized_item(item)

    async def clear_session(self) -> None:
        async with self._lock:
            await self._session.clear_session()
            if self.archive_store is not None and self.scope is not None:
                self.archive_store.clear_conversation_archive(self.scope)
            self._index = _ArchiveIndex()
            self._revision += 1

    def close(self) -> None:
        self._session.close()

    def summary_payload(self) -> dict[str, object] | None:
        if self.scope is None or self._index is None:
            return None
        if self._summary is None or self._summary[0] != self._revision:
            self._summary = (self._revision, {
                **self._index.summary(self.scope, max_turns=self.max_turns),
                "archive_revision": self._revision,
                "archive_item_count": len(self._index.items),
            })
        return dict(self._summary[1])

    def query_archive(
        self,
        *,
        query: str = "",
        start: int = 0,
        limit: int = 5,
    ) -> dict[str, object]:
        turns = self._archived_turns()
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
        turns = self._archived_turns()
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

    def _archived_turns(self) -> list[dict[str, object]]:
        if self.scope is None or self._index is None:
            return []
        return self._index.closed_turns(self.scope)

    async def _loaded(self) -> _ArchiveIndex:
        """Read the stored stream once; callers hold ``self._lock``."""
        if self._index is None:
            items = await self._session.get_items(limit=None)
            sanitized, changed = await asyncio.to_thread(_sanitized_stream, items)
            if changed:
                # History written before a sanitizer rule existed.
                await self._session.clear_session()
                if sanitized:
                    await self._session.add_items(sanitized)
            self._index = await asyncio.to_thread(_ArchiveIndex, sanitized)
            self._revision += 1
            if self.archive_store is not None and self.scope is not None:
                # Earlier versions kept a second, whole copy of the stream
                # here and rewrote it on every write.
                await asyncio.to_thread(self.archive_store.clear_conversation_archive, self.scope)
        return self._index


class _ArchiveIndex:
    """Turn boundaries and sizes of the stored stream, kept as items arrive.

    The summary and the archive tools read the same facts the whole-stream
    computation produced: closed turns (tool calls paired, a final answer after
    the last call), item and character counts of the live window and of the
    archive. Turns before the last one never change once the next user item
    arrives, so their state is fixed then; only the open turn is updated.
    """

    def __init__(self, items: list[Any] | tuple[Any, ...] = ()) -> None:
        self.items: list[Any] = []
        self._chars: list[int] = [0]
        self._starts: list[int] = []
        self._sealed: list[bool] = []
        self._turn_summaries: dict[int, dict[str, object]] = {}
        self._open = _OpenTurn()
        for item in items:
            self.append(item)

    def append(self, item: Any) -> None:
        index = len(self.items)
        self.items.append(item)
        self._chars.append(self._chars[-1] + len(json.dumps(item, ensure_ascii=False, sort_keys=True)))
        if _session_item_role(item) == "user":
            if self._starts:
                self._sealed.append(self._open.closed())
            self._starts.append(index)
            self._open = _OpenTurn()
        elif self._starts:
            self._open.add(index - self._starts[-1], item)

    def closed_turns(self, scope: RuntimeScope | None) -> list[dict[str, object]]:
        scope_key = "memory" if scope is None else scope.key
        turns = []
        for ordinal, start in enumerate(self._starts, start=1):
            sealed = ordinal <= len(self._sealed)
            if not (self._sealed[ordinal - 1] if sealed else self._open.closed()):
                continue
            end = self._starts[ordinal] if sealed else len(self.items)
            turns.append({
                "handle": f"conversation:{scope_key}:turn:{ordinal}",
                "ordinal": ordinal,
                "items": self.items[start:end],
            })
        return turns

    def summary(self, scope: RuntimeScope, *, max_turns: int) -> dict[str, object]:
        turns = self.closed_turns(scope)
        compacted = turns[:-max_turns] if len(turns) > max_turns else []
        live_start = self._starts[-max_turns] if len(self._starts) > max_turns else 0
        recent = []
        for turn in compacted[-20:]:
            # Compacted turns are never the open one, so their items are fixed.
            ordinal = int(turn["ordinal"])
            if ordinal not in self._turn_summaries:
                self._turn_summaries[ordinal] = _turn_summary(turn)
            recent.append(self._turn_summaries[ordinal])
        return {
            "scope_key": scope.key,
            "total_closed_turns": len(turns),
            "live_turns": min(len(turns), max_turns),
            "compacted_turns": len(compacted),
            "covered_turn_handles": [turn["handle"] for turn in compacted],
            "recent_compacted": copy.deepcopy(recent),
            "live_item_count": len(self.items) - live_start,
            "live_item_chars": self._list_chars(live_start, len(self.items)),
            "archive_item_chars": self._list_chars(0, len(self.items)),
            "complete": True,
        }

    def _list_chars(self, start: int, end: int) -> int:
        """``len(json.dumps(items[start:end], ensure_ascii=False, sort_keys=True))``."""
        count = end - start
        if count <= 0:
            return 2
        return self._chars[end] - self._chars[start] + 2 * (count - 1) + 2


class _OpenTurn:
    """What decides whether the newest turn is closed, kept item by item."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.outputs: Counter[str] = Counter()
        self.unnamed = False
        self.protocol = False
        self.last_protocol = 0
        self.last_assistant = -1

    def add(self, offset: int, item: Any) -> None:
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            self.calls[call_id] += 1
        elif kind == "function_call_output":
            call_id = str(item.get("call_id") or "")
            self.outputs[call_id] += 1
        if kind in {"function_call", "function_call_output"}:
            self.protocol = True
            self.unnamed = self.unnamed or not call_id
            self.last_protocol = offset
        if _session_item_role(item) == "assistant":
            self.last_assistant = offset

    def closed(self) -> bool:
        protocol_closed = not self.protocol or (not self.unnamed and self.calls == self.outputs)
        return protocol_closed and self.last_assistant > self.last_protocol


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


def bounded_model_input(data: CallModelData[Any], *, live_context: str = "") -> ModelInputData:
    """Bound one long SDK run while retaining the current request and closed tool pairs.

    Instructions are returned untouched: they are the cached head of the request
    (``prompt_cache.py``). ``live_context`` and the window notice, which both move
    between calls, ride together as the one trailing CURRENT_CONTEXT item.
    """

    original = data.model_data.input
    bounded = bounded_model_input_items(original)
    notice = None
    if len(bounded) != len(original):
        notice = (
            "MODEL_INPUT_WINDOW complete=true "
            f"omitted_items={len(original) - len(bounded)} retained_items={len(bounded)}. "
            "Current task, Body state, active Skills, and recent complete tool results in this "
            "request are authoritative. Use conversation archive tools only when an older exact "
            "exchange is materially needed."
        )
    context_item = current_context_item(live_context, notice or "")
    if context_item is None:
        return data.model_data
    return ModelInputData(
        input=[*bounded, context_item],
        instructions=data.model_data.instructions,
    )


def bounded_model_input_items(
    items: list[Any],
    *,
    max_items: int = MODEL_INPUT_MAX_ITEMS,
    max_chars: int = MODEL_INPUT_MAX_CHARS,
) -> list[Any]:
    """Keep a recent coherent suffix without splitting function call/result pairs."""

    max_items = max(1, int(max_items))
    max_chars = max(1, int(max_chars))
    if len(items) <= max_items and model_input_chars(items) <= max_chars:
        return list(items)

    latest_user = next(
        (index for index in range(len(items) - 1, -1, -1) if _session_item_role(items[index]) == "user"),
        None,
    )
    selected: set[int] = set()
    if latest_user is not None:
        selected.add(latest_user)

    pair_indices: dict[str, set[int]] = {}
    for index, item in enumerate(items):
        if _session_item_type(item) not in {"function_call", "function_call_output"}:
            continue
        call_id = _session_item_call_id(item)
        if call_id:
            pair_indices.setdefault(call_id, set()).add(index)

    selected_chars = sum(_model_item_chars(items[index]) for index in selected)
    retained_suffix_item = False
    for index in range(len(items) - 1, -1, -1):
        if index in selected:
            continue
        group = {index}
        call_id = _session_item_call_id(items[index])
        if call_id and _session_item_type(items[index]) in {"function_call", "function_call_output"}:
            group.update(pair_indices.get(call_id, ()))
        group.difference_update(selected)
        group_chars = sum(_model_item_chars(items[item_index]) for item_index in group)
        exceeds_window = (
            len(selected) + len(group) > max_items
            or selected_chars + group_chars > max_chars
        )
        if exceeds_window and retained_suffix_item:
            break
        selected.update(group)
        selected_chars += group_chars
        retained_suffix_item = True

    return [item for index, item in enumerate(items) if index in selected]


def model_input_chars(items: list[Any]) -> int:
    return sum(_model_item_chars(item) for item in items)


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


def _session_item_type(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("type") or "")
    return str(getattr(item, "type", "") or "")


def _session_item_call_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("call_id") or item.get("id") or "")
    return str(getattr(item, "call_id", None) or getattr(item, "id", None) or "")


def _model_item_chars(item: Any) -> int:
    return len(json.dumps(_json_item(item), ensure_ascii=False, sort_keys=True))


def _json_item(item: Any) -> object:
    if isinstance(item, dict):
        return copy.deepcopy(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return json.loads(json.dumps(item, ensure_ascii=False, default=str))


def _sanitized_item(item: Any) -> Any:
    return sanitize_observation(_json_item(item))


def _sanitized_stream(items: list[Any]) -> tuple[list[Any], bool]:
    """Sanitize a stored stream and say whether any item changed.

    Item by item, so a thread doing it lets the event loop run in between.
    """
    sanitized: list[Any] = []
    changed = False
    for item in items:
        stored = _json_item(item)
        clean = _sanitized_item(stored)
        changed = changed or clean != stored
        sanitized.append(clean)
    return sanitized, changed


def _stored_item(item: Any) -> Any:
    """The sanitized item as it reads back from the SDK table."""
    return json.loads(json.dumps(_sanitized_item(item), ensure_ascii=False))


def _copied_items(items: list[Any]) -> list[Any]:
    return [_json_item(item) for item in items]


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
    "MODEL_INPUT_MAX_CHARS",
    "MODEL_INPUT_MAX_ITEMS",
    "PersistentWindowedConversationSession",
    "WindowedConversationSession",
    "bounded_model_input",
    "bounded_model_input_items",
    "bounded_session_input",
    "model_input_chars",
]
