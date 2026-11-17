"""Shared bounded-projection helpers for model-visible tool summaries.

These utilities were extracted from ``app/runner.py`` so that per-tool
observation projectors can live NEXT TO their tool registrations
(brain-cognitive-framework.md §12 H1) without importing the runner. The
runner imports them back for its generic projection path; owning modules
import them for their registered projectors. This module must stay free of
runner, registry-construction, and Body imports.
"""

from __future__ import annotations

import json

from minebot.app.observability import sanitize_observation
from minebot.contract import JsonObject
from minebot.game.real_client_frame import without_ambient_state


def shorten(text: str, *, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def bounded_summary_value(value: object) -> object:
    if isinstance(value, dict):
        out: JsonObject = {}
        for key, item in list(value.items())[:12]:
            out[str(key)] = bounded_summary_value(item)
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


def encoded_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def answer_value(value: object, *, budget: int) -> tuple[object, bool]:
    """The answer a read tool was asked for: whole when it fits, bounded otherwise.

    Returns the value to show and whether it is complete.
    """
    if encoded_chars(value) <= budget:
        return value, True
    return bounded_summary_value(value), False


def blocks_by_kind(blocks: list[object]) -> list[JsonObject]:
    """Block facts grouped by what they are, so the positions carry the detail.

    A flat list repeats type, state and properties for every block; in a
    neighbourhood most blocks are a few kinds, so grouping keeps every block.
    """
    groups: dict[str, JsonObject] = {}
    loose: list[JsonObject] = []
    for block in blocks:
        if not isinstance(block, dict) or not all(isinstance(block.get(axis), int) for axis in ("x", "y", "z")):
            if isinstance(block, dict):
                loose.append(block)
            continue
        described = {key: item for key, item in block.items() if key not in {"x", "y", "z"}}
        kind = json.dumps(described, ensure_ascii=False, sort_keys=True, default=str)
        group = groups.setdefault(kind, {**described, "positions": []})
        group["positions"].append([block["x"], block["y"], block["z"]])
    return [*groups.values(), *loose]


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
    "answer_value",
    "blocks_by_kind",
    "bounded_summary_value",
    "encoded_chars",
    "projection_values_equal",
    "shorten",
    "top_reasons",
    "without_ambient_state",
]
