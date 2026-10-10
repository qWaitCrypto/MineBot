"""Prompt-cache layout shared by the runner and the provider adapters.

Every provider caches the longest identical prefix of ``tools -> instructions ->
input``. A request is therefore laid out stable-first: instructions carry only
rules that do not move between model calls, and everything live (goal, body
state, stance, input-window notices) rides as one trailing ``CURRENT_CONTEXT:``
input item. Providers with automatic prefix caching (OpenAI, Gemini, DeepSeek,
...) then hit on the whole history; adapters that need explicit breakpoints
(Anthropic) place them directly in front of that item.
"""

from __future__ import annotations

from typing import Any

CURRENT_CONTEXT_HEADER = "CURRENT_CONTEXT:"


def current_context_item(*sections: str) -> dict[str, str] | None:
    """The one volatile trailing input item, or None when nothing is live."""
    body = "\n\n".join(section for section in sections if section)
    if not body:
        return None
    return {"role": "user", "content": f"{CURRENT_CONTEXT_HEADER}\n{body}"}


def is_current_context_text(text: Any) -> bool:
    return isinstance(text, str) and text.startswith(CURRENT_CONTEXT_HEADER)


def is_current_context_item(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and item.get("role") == "user"
        and is_current_context_text(item.get("content"))
    )


__all__ = [
    "CURRENT_CONTEXT_HEADER",
    "current_context_item",
    "is_current_context_item",
    "is_current_context_text",
]
