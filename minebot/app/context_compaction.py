"""Threshold compaction of the model input for SDK-kernel runs.

The model reads the whole conversation, which only ever grows at its end, so
every request repeats the previous one as a prefix and provider prompt caches
hit. A window that slides on each request moves the start of that prefix and
makes every request a full cache write.

When the estimated request reaches the threshold (128k tokens by default), the
older part is replaced once by a summary. The result is again a stable prefix
until the next compaction. The stored conversation is not rewritten: this is a
view, and the archive tools still read the original turns.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_COMPACT_THRESHOLD_TOKENS = 128_000
# Recent items kept verbatim so the model continues from real tool results.
DEFAULT_TAIL_CHARS = 24_000
# Rough characters per token before a provider has reported real usage.
CHARS_PER_TOKEN = 3.0
# Tool definitions are not visible to the estimator before the first usage.
UNSEEN_STATIC_TOKENS = 16_000
TRANSCRIPT_FIELD_CHARS = 4_000
SUMMARY_HEADER = "CONVERSATION_SUMMARY (historical, not a new request):"

SUMMARY_INSTRUCTIONS = (
    "Summarize this ongoing Minecraft Agent conversation for its next turn. Do not act or call tools. "
    "Preserve player goals, additions and constraints, the current short plan, exact useful coordinates "
    "and entity IDs, verified work, materials, unresolved blockers, failed approaches and why they failed. "
    "Keep historical observations distinct from current facts. Preserve any player cancellation. "
    "The latest CURRENT_CONTEXT is authoritative for the current goal, plan and Body state. "
    "Omit repetitive polling, superseded state and internal bookkeeping. Return only a concise continuation summary."
)

Summarize = Callable[[str, str], Awaitable[str]]


def item_fingerprint(item: Any) -> str:
    return hashlib.sha256(
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _chars(items: list[Any]) -> int:
    return sum(len(json.dumps(item, ensure_ascii=False, default=str)) for item in items)


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _is_user(item: Any) -> bool:
    return _field(item, "role") == "user"


def _is_summary(item: Any) -> bool:
    content = _field(item, "content")
    return _is_user(item) and isinstance(content, str) and content.startswith(SUMMARY_HEADER)


def _transcript(items: list[Any]) -> str:
    """Items as JSON with oversized fields cut; the archive keeps the originals."""

    def shorten(value: Any) -> Any:
        if isinstance(value, str) and len(value) > TRANSCRIPT_FIELD_CHARS:
            return value[:TRANSCRIPT_FIELD_CHARS] + f"...[{len(value) - TRANSCRIPT_FIELD_CHARS} chars omitted]"
        if isinstance(value, dict):
            return {key: shorten(inner) for key, inner in value.items() if key != "encrypted_content"}
        if isinstance(value, list):
            return [shorten(inner) for inner in value]
        return value

    return json.dumps([shorten(item) for item in items], ensure_ascii=False, default=str)


@dataclass(frozen=True)
class _Compaction:
    anchor: str  # fingerprint of the first item kept verbatim
    summary: str


class ConversationCompactor:
    """Keeps one compaction boundary and renders the stable view behind it."""

    def __init__(
        self,
        *,
        summarize: Summarize,
        emit: Callable[..., None] = lambda *args, **kwargs: None,
        threshold_tokens: int = DEFAULT_COMPACT_THRESHOLD_TOKENS,
        tail_chars: int = DEFAULT_TAIL_CHARS,
    ) -> None:
        self.summarize = summarize
        self.emit = emit
        # Zero keeps the whole conversation and never summarizes.
        self.threshold_tokens = max(0, int(threshold_tokens))
        self.tail_chars = max(1, int(tail_chars))
        self._compaction: _Compaction | None = None
        self._last_view_chars: int | None = None
        self._last_input_tokens: int | None = None

    def note_usage(self, input_tokens: int) -> None:
        """Real size of the request built from the last view."""
        if input_tokens > 0 and self._last_view_chars is not None:
            self._last_input_tokens = int(input_tokens)

    def estimate_tokens(self, view: list[Any], static_chars: int) -> int:
        chars = _chars(view)
        if self._last_input_tokens is not None and self._last_view_chars is not None:
            return max(0, int(self._last_input_tokens + (chars - self._last_view_chars) / CHARS_PER_TOKEN))
        return int((chars + static_chars) / CHARS_PER_TOKEN) + UNSEEN_STATIC_TOKENS

    async def view(self, items: list[Any], *, static_chars: int = 0) -> list[Any]:
        """The items to send: everything, or summary plus what followed it."""
        items = list(items)
        start = self._anchor_index(items)
        view = self._render(items, start)
        estimate = self.estimate_tokens(view, static_chars)
        over = bool(self.threshold_tokens) and estimate >= self.threshold_tokens
        if over:
            compacted = await self._compact(items, start, estimate)
            if compacted is not None:
                view = compacted
        self._last_view_chars = _chars(view)
        if over:
            # The calibration belonged to the larger request.
            self._last_input_tokens = None
        return view

    # -- internals -------------------------------------------------------------

    def _anchor_index(self, items: list[Any]) -> int | None:
        if self._compaction is None:
            return None
        for index, item in enumerate(items):
            if item_fingerprint(item) == self._compaction.anchor:
                return index
        # The stored history no longer contains the boundary (cleared or
        # trimmed elsewhere): the summary describes items nobody can see.
        self.emit("conversation_compaction_dropped", reason="anchor_missing")
        self._compaction = None
        self._last_input_tokens = None
        return None

    def _render(self, items: list[Any], start: int | None) -> list[Any]:
        if start is None or self._compaction is None:
            return items
        head = _player_messages(items[:start])
        return [*head, {"role": "user", "content": f"{SUMMARY_HEADER}\n{self._compaction.summary}"}, *items[start:]]

    async def _compact(self, items: list[Any], start: int | None, estimate: int) -> list[Any] | None:
        first = start or 0
        tail = self._tail_start(items, first)
        if tail is None:
            self.emit("conversation_compaction_deferred", reason="nothing_older_than_tail",
                      estimated_tokens=estimate)
            return None
        older = items[first:tail]
        previous = self._compaction.summary if self._compaction is not None else ""
        self.emit("conversation_compaction_started", estimated_tokens=estimate,
                  threshold_tokens=self.threshold_tokens, summarized_items=len(older),
                  kept_items=len(items) - tail)
        try:
            summary = (await self.summarize(previous, _transcript(older))).strip()
        except Exception as exc:
            summary = ""
            self.emit("conversation_compaction_failed", error_type=type(exc).__name__, message=str(exc)[:300])
        if not summary:
            # The request must still fit. Say plainly that the older part is
            # gone rather than let the model assume it saw everything.
            summary = (previous + "\n" if previous else "") + (
                f"[{len(older)} older conversation items could not be summarized and are omitted. "
                "Use the conversation archive tools if an older exact exchange is needed.]"
            )
        self._compaction = _Compaction(item_fingerprint(items[tail]), summary)
        view = self._render(items, tail)
        self.emit("conversation_compaction_finished", before_items=len(items), after_items=len(view),
                  before_estimated_tokens=estimate, after_chars=_chars(view), summary_chars=len(summary))
        return view

    def _tail_start(self, items: list[Any], first: int) -> int | None:
        """First index of the verbatim tail; never splits a tool call from its result."""
        budget, index = self.tail_chars, len(items)
        while index > first + 1:
            size = _chars([items[index - 1]])
            if size > budget and index < len(items):
                break
            budget -= size
            index -= 1
        calls = {
            _field(item, "call_id"): position
            for position, item in enumerate(items) if _field(item, "type") == "function_call"
        }
        moved = True
        while moved:
            moved = False
            for item in items[index:]:
                call = calls.get(_field(item, "call_id")) if _field(item, "type") == "function_call_output" else None
                if call is not None and call < index:
                    index, moved = call, True
                    break
            # A reasoning item belongs to the output item that follows it.
            while index > first and _field(items[index - 1], "type") == "reasoning":
                index, moved = index - 1, True
        if index <= first or index >= len(items):
            return None
        return index


def _player_messages(items: list[Any]) -> list[Any]:
    """What the player actually said, in order, without repeats or old summaries."""
    kept: list[Any] = []
    for item in items:
        if not _is_user(item) or _is_summary(item):
            continue
        if kept and _field(kept[-1], "content") == _field(item, "content"):
            continue
        kept.append(item)
    return kept


__all__ = [
    "ConversationCompactor",
    "DEFAULT_COMPACT_THRESHOLD_TOKENS",
    "SUMMARY_HEADER",
    "SUMMARY_INSTRUCTIONS",
    "item_fingerprint",
]
