"""What each Responses request repeated of the one before it.

A provider's prompt cache can only hit on the longest identical prefix of
``tools -> instructions -> input``. The model reports how much it hit
(``cached_tokens``); this reports what we sent, so a miss can be told apart:
did our request change early, or did the provider not cache a prefix we
repeated? Hashes only; no request text is recorded.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from agents import OpenAIResponsesModel


def _fp(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def _kind(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("type") or item.get("role") or "item")
    return "text"


class RequestLayoutTracker:
    """Compares each request body with the previous one that had the same tools."""

    def __init__(self) -> None:
        self._previous: dict[str, list[str]] = {}
        self._previous_kinds: dict[str, list[str]] = {}
        self.last: dict[str, object] | None = None

    def observe(self, request: httpx.Request) -> None:
        if not str(request.url.path).endswith("/responses"):
            return
        try:
            body = json.loads(request.content.decode())
            tools = body.get("tools") or []
            items = body.get("input")
            if isinstance(items, str):
                items = [{"role": "user", "content": items}]
            items = list(items or [])
            hashes = [_fp(item) for item in items]
            kinds = [_kind(item) for item in items]
            head = _fp(tools) + ":" + _fp(body.get("instructions"))
        except Exception:
            self.last = None
            return
        layout: dict[str, object] = {
            "tools": len(tools), "items": len(items), "head": head,
        }
        before = self._previous.get(_fp(tools))
        if before is not None:
            shared = 0
            for ours, theirs in zip(hashes, before):
                if ours != theirs:
                    break
                shared += 1
            layout["previous_items"] = len(before)
            layout["items_shared_with_previous"] = shared
            # The previous request ends with its own CURRENT_CONTEXT item, which
            # is replaced; sharing everything before it is the best possible.
            layout["lost_items"] = max(0, len(before) - 1 - shared)
            if shared < len(before) - 1 and shared < len(items):
                layout["first_difference"] = {
                    "index": shared,
                    "was": self._previous_kinds[_fp(tools)][shared],
                    "now": kinds[shared],
                }
        self._previous[_fp(tools)] = hashes
        self._previous_kinds[_fp(tools)] = kinds
        self.last = layout


class LayoutReportingResponsesModel(OpenAIResponsesModel):
    """Responses model that can say how its last request related to the one before."""

    def __init__(self, *args: Any, tracker: RequestLayoutTracker, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tracker = tracker

    @property
    def last_request_layout(self) -> dict[str, object] | None:
        return self._tracker.last


__all__ = ["LayoutReportingResponsesModel", "RequestLayoutTracker"]
