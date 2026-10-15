"""Small, user-authored idle-life profile loader."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Mapping

def load_profile(path: str | None = None) -> dict[str, Any]:
    raw = path or os.environ.get("MINEBOT_LIFE_PROFILE")
    if not raw:
        return {}
    try:
        value = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}

def life_intent(profile: Mapping[str, Any]) -> str:
    parts = []
    for key in ("routine", "personality"):
        if isinstance(profile.get(key), str) and profile[key].strip():
            parts.append(profile[key].strip())
    relationships = profile.get("relationships")
    if isinstance(relationships, list):
        parts.extend(str(item.get("note")) for item in relationships
                     if isinstance(item, dict) and item.get("note"))
    return " ".join(parts)
