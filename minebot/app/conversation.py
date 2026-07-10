"""Provider-portable, bounded conversation storage for openai-agents runs."""

from __future__ import annotations

import copy
import threading
from typing import Any
from uuid import uuid4

from agents import SessionSettings

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
                items = self._items[-limit:]
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


def _session_item_role(item: Any) -> object:
    if isinstance(item, dict):
        return item.get("role")
    return getattr(item, "role", None)


__all__ = [
    "CONVERSATION_WINDOW_TURNS",
    "WindowedConversationSession",
    "bounded_session_input",
]
