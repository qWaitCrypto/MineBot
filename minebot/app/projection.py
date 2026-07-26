"""Shared bounded-projection helpers for model-visible tool summaries.

These utilities were extracted from ``app/runner.py`` so that per-tool
observation projectors can live NEXT TO their tool registrations
(brain-cognitive-framework.md §12 H1) without importing the runner. The
runner imports them back for its generic projection path; owning modules
import them for their registered projectors. This module must stay free of
runner, registry-construction, and Body imports.
"""

from __future__ import annotations

from minebot.app.observability import sanitize_observation
from minebot.contract import JsonObject


def shorten(text: str, *, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def bounded_summary_value(value: object) -> object:
    if isinstance(value, dict):
        out: JsonObject = {}
        for key, item in list(value.items())[:12]:
            if isinstance(item, (dict, list, tuple)):
                out[str(key)] = bounded_summary_value(item)
            else:
                out[str(key)] = item
        return out
    if isinstance(value, (list, tuple)):
        if len(value) <= 8 and all(not isinstance(item, (dict, list, tuple)) for item in value):
            return list(value)
        return {
            "count": len(value),
            "sample": [bounded_summary_value(item) for item in list(value)[:3]],
        }
    if isinstance(value, str):
        return shorten(value, limit=300)
    return value


def top_reasons(items: list[object]) -> list[str]:
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [f"{reason}:{count}" for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:6]]


def projection_values_equal(source: object, projected: object) -> bool:
    try:
        return sanitize_observation(source) == sanitize_observation(projected)
    except Exception:
        return False


__all__ = [
    "bounded_summary_value",
    "projection_values_equal",
    "shorten",
    "top_reasons",
]
