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

from minebot.brain.context import HARNESS_FACT_PREFIX

DEFAULT_COMPACT_THRESHOLD_TOKENS = 128_000
# Recent items kept verbatim so the model continues from real tool results.
DEFAULT_TAIL_CHARS = 24_000
# Rough characters per token before a provider has reported real usage.
CHARS_PER_TOKEN = 3.0
# Tool definitions are not visible to the estimator before the first usage.
UNSEEN_STATIC_TOKENS = 16_000
TRANSCRIPT_FIELD_CHARS = 4_000
# Largest single item sent to the model. Projected tool results stay well under
# it; only a producer that skipped projection reaches it.
MAX_ITEM_CHARS = 32_000
SUMMARY_HEADER = "CONVERSATION_SUMMARY (historical, not a new request):"

# Written for an agent that lives in a Minecraft world, not one that edits a
# repository. What it loses when history is cut is where things are, what the
# world already taught it, and what it has really built: none of that can be
# read back from a file. What it does not need is a copy of anything it is
# handed fresh with every request.
SUMMARY_INSTRUCTIONS = """\
You are writing the memory an agent playing Minecraft through a body will continue from. The older part of \
its conversation is about to be removed and replaced by what you write. Do not act, do not call tools, do not \
address the player. Write the notes; nothing else.

The next request also contains a live context block: the current goal and task/checkpoint state, position, \
health, hunger, inventory, plan, and any work still running. Do not copy those live values. A number from the \
old conversation that the agent will see live (an item count, a position, a health value) is stale the moment \
you write it and must be left out. Keep player goals and constraints only when they are not represented by \
that live context.

Keep what cannot be looked up again without redoing work. Use these headings, in this order, and leave a \
heading out when there is nothing true to put under it.

PLACES
Coordinates worth returning to, each with what is there and how it was learned: the chosen build site and \
its bounds, crafting table, furnace, chests and beds that were placed, resource spots (trees, stone, sand, \
ore, water, animals) with the kind found, cave or mine entrances, the spawn point, a death spot with what \
was dropped. Exact integers as they appeared. A place the agent only guessed at is marked guessed.

STORED AND PLACED
What was put in a chest or furnace or left on the ground and where. What has been built so far, as observed \
facts: which parts stand, of what material, from which corner to which corner, and which parts were \
verified by reading the world rather than assumed from a successful tool call. What is known to be missing \
from the build.

HOW THIS WORLD AND BODY BEHAVE
What the agent found out by trying: an approach that failed and the reason given, a target that could not \
be reached and why, a recipe that was locked and what unlocked it, a tool that broke, a tool call whose \
arguments had to be a particular way, a kind of request that works better in one call than in several. \
One line each. These keep the agent from paying for the same lesson twice, so prefer them over narrative.

DECISIONS
Choices already made and the reason, so they are not reopened: the design chosen for the build, materials \
decided on, an order of work, something the player agreed to or ruled out beyond their literal messages, \
a promise the agent made to the player and has not kept yet.

WHERE THINGS STAND
In a few lines: what was being worked on when these notes were written, what the last finished step really \
achieved, and the next step the agent intended. If the conversation ends with work the agent started and \
whose result had not arrived, say so with its id.

Rules.
Facts are dated by order, not by clock: when two observations disagree, the later one wins and the earlier \
is dropped, not listed beside it.
State how each fact is known when it matters: seen in a tool result, told by the player, or assumed.
Animals, monsters and dropped items move or vanish; mention one only if it mattered and say it may be gone.
Leave out polling, repeated status reads, pathfinding detail, tool output that only confirmed what was \
expected, and the agent's own commentary.
If PREVIOUS_SUMMARY is given, these notes replace it: carry forward what is still true, correct what the \
newer conversation overturned, and drop what no longer matters. Do not append a second set of notes.
Write in the language the player uses. Be brief: a line per fact, no introduction, no closing remark.\
"""

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


def bound_item(item: Any, limit_chars: int = MAX_ITEM_CHARS) -> tuple[Any, int]:
    """One conversation item with any oversized text cut; returns it and the chars removed.

    Every producer is expected to hand the model a projected result. This is
    the last line behind them: one item that slipped through must not become
    the whole request. The cut is a pure function of the item, so the same
    item is cut the same way on every request and the prefix stays stable.
    """
    if limit_chars <= 0 or not isinstance(item, dict) or _chars([item]) <= limit_chars:
        return item, 0
    removed = 0

    def cut(value: Any) -> Any:
        nonlocal removed
        if isinstance(value, str) and len(value) > limit_chars:
            removed += len(value) - limit_chars
            return value[:limit_chars] + (
                f"\n...[{len(value) - limit_chars} characters omitted: this message was larger than one "
                "message may be. Read the source again with a narrower request if the rest is needed.]"
            )
        if isinstance(value, dict):
            return {key: cut(inner) for key, inner in value.items()}
        if isinstance(value, list):
            return [cut(inner) for inner in value]
        return value

    bounded = cut(item)
    return (bounded, removed) if removed else (item, 0)


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
        # A compaction has to remove at least this much to be worth a request.
        self.min_gain_chars = min(self.tail_chars, int(self.threshold_tokens * CHARS_PER_TOKEN / 8))
        self._compaction: _Compaction | None = None
        self._last_view_chars: int | None = None
        self._last_input_tokens: int | None = None

    def note_usage(self, input_tokens: int) -> None:
        """Real size of the request built from the last view."""
        if input_tokens > 0 and self._last_view_chars is not None:
            self._last_input_tokens = int(input_tokens)

    def estimate_tokens(self, view: list[Any], static_chars: int, live_chars: int = 0) -> int:
        chars = _chars(view) + live_chars
        if self._last_input_tokens is not None and self._last_view_chars is not None:
            return max(0, int(self._last_input_tokens + (chars - self._last_view_chars) / CHARS_PER_TOKEN))
        return int((chars + static_chars) / CHARS_PER_TOKEN) + UNSEEN_STATIC_TOKENS

    async def view(self, items: list[Any], *, static_chars: int = 0, live_chars: int = 0) -> list[Any]:
        """The items to send: everything, or summary plus what followed it.

        ``live_chars`` is what the caller appends after the view on every
        request (the current context); it is part of the size being judged.
        """
        items = list(items)
        start = self._anchor_index(items)
        view = self._render(items, start)
        estimate = self.estimate_tokens(view, static_chars, live_chars)
        over = bool(self.threshold_tokens) and estimate >= self.threshold_tokens
        if over:
            compacted = await self._compact(items, start, estimate)
            if compacted is not None:
                view = compacted
        self._last_view_chars = _chars(view) + live_chars
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
                      estimated_size=estimate)
            return None
        older = items[first:tail]
        if _chars(older) < self.min_gain_chars:
            # What is large is the part that stays. Summarizing the little
            # that is older would cost a request and the cache, every request.
            self.emit("conversation_compaction_deferred", reason="little_to_summarize",
                      estimated_size=estimate, older_chars=_chars(older))
            return None
        previous = self._compaction.summary if self._compaction is not None else ""
        self.emit("conversation_compaction_started", estimated_size=estimate,
                  threshold=self.threshold_tokens, summarized_items=len(older),
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
        if len(summary) > MAX_ITEM_CHARS:
            summary = (summary[:MAX_ITEM_CHARS]
                       + f"\n[{len(summary) - MAX_ITEM_CHARS} further characters of this summary omitted]")
        self._compaction = _Compaction(item_fingerprint(items[tail]), summary)
        view = self._render(items, tail)
        self.emit("conversation_compaction_finished", before_items=len(items), after_items=len(view),
                  before_estimated_size=estimate, after_chars=_chars(view), summary_chars=len(summary))
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


def _player_part(item: Any) -> Any | None:
    """The item reduced to what the player said, or None if it is all harness facts.

    Harness facts enter the conversation as user-role input too. They are
    history like any tool result and belong in the summary; kept verbatim they
    would make every compaction carry all of them forward.
    """
    content = _field(item, "content")
    if not isinstance(content, str) or HARNESS_FACT_PREFIX not in content:
        return item
    spoken = "\n".join(line for line in content.split("\n") if not line.startswith(HARNESS_FACT_PREFIX))
    if not spoken.strip():
        return None
    return {**item, "content": spoken} if isinstance(item, dict) else item


def _player_messages(items: list[Any]) -> list[Any]:
    """What the player actually said, in order, without repeats or old summaries."""
    kept: list[Any] = []
    for item in items:
        if not _is_user(item) or _is_summary(item):
            continue
        item = _player_part(item)
        if item is None:
            continue
        if kept and _field(kept[-1], "content") == _field(item, "content"):
            continue
        kept.append(item)
    return kept


__all__ = [
    "ConversationCompactor",
    "MAX_ITEM_CHARS",
    "bound_item",
    "DEFAULT_COMPACT_THRESHOLD_TOKENS",
    "SUMMARY_HEADER",
    "SUMMARY_INSTRUCTIONS",
    "item_fingerprint",
]
