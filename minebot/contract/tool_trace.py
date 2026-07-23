"""Stable tool trace identifiers shared by runtime and composition layers."""

from __future__ import annotations

import json
from hashlib import sha256


_TACTIC_NUMERIC_KNOBS = frozenset(
    {
        "budget",
        "count",
        "deadline",
        "distance",
        "find_limit",
        "grid_radius",
        "limit",
        "max_distance",
        "max_pages",
        "max_regions",
        "max_steps",
        "page_limit",
        "radius",
        "scan_radius",
        "search_radius",
        "start",
        "timeout",
        "timeout_s",
        "y_radius",
        "yRadius",
    }
)
_TACTIC_POSITION_KEYS = frozenset(
    {
        "block",
        "center",
        "destination",
        "from",
        "goal",
        "origin",
        "pos",
        "position",
        "target",
        "to",
        "x",
        "y",
        "z",
    }
)


def canonical_args_hash(payload: object) -> str:
    return sha256(canonical_tool_arguments(payload).encode("utf-8")).hexdigest()


def canonical_args_hash_from_json(input_json: str) -> str:
    return sha256(canonical_tool_arguments_from_json(input_json).encode("utf-8")).hexdigest()


def canonical_tool_arguments(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_tool_arguments_from_json(input_json: str) -> str:
    if not input_json:
        return "{}"
    try:
        parsed = json.loads(input_json)
    except json.JSONDecodeError:
        return f"invalid:{input_json}"
    return canonical_tool_arguments(parsed)


def tool_tactic_signature(tool_name: str, payload: object) -> str:
    semantic = _semantic_tactic_payload(payload)
    canonical = canonical_tool_arguments(semantic)
    digest = sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{digest}"


def tool_tactic_signature_from_json(tool_name: str, input_json: str) -> str:
    if not input_json:
        payload: object = {}
    else:
        try:
            payload = json.loads(input_json)
        except json.JSONDecodeError:
            payload = {"invalid_json": True}
    return tool_tactic_signature(tool_name, payload)


def _semantic_tactic_payload(value: object, *, key: str | None = None) -> object:
    if isinstance(value, dict):
        semantic: dict[str, object] = {}
        for item_key, item_value in sorted(value.items()):
            clean_key = str(item_key)
            if _is_tactic_knob_key(clean_key):
                continue
            semantic[clean_key] = _semantic_tactic_payload(item_value, key=clean_key)
        return semantic
    if isinstance(value, list):
        if key is not None and _is_tactic_knob_key(key):
            return []
        return [_semantic_tactic_payload(item, key=key) for item in value]
    if isinstance(value, (int, float)) and key is not None and _is_tactic_knob_key(key):
        return None
    return value


def _is_tactic_knob_key(key: str) -> bool:
    lowered = key.casefold()
    if lowered in _TACTIC_POSITION_KEYS or lowered in _TACTIC_NUMERIC_KNOBS:
        return True
    return lowered.endswith(("_pos", "_position", "_radius", "_limit", "_timeout", "_timeout_s"))


__all__ = [
    "canonical_args_hash",
    "canonical_args_hash_from_json",
    "canonical_tool_arguments",
    "canonical_tool_arguments_from_json",
    "tool_tactic_signature",
    "tool_tactic_signature_from_json",
]
