"""Durable observation sinks for Agent Phase 1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class ObservationSink(Protocol):
    def write(self, record: dict[str, object]) -> None: ...

    def close(self) -> None: ...


class JsonlObservationSink:
    """Append one sanitized observation record per line."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, record: dict[str, object]) -> None:
        safe = sanitize_observation(record)
        self._fh.write(json.dumps(safe, ensure_ascii=True, sort_keys=True, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def sanitize_observation(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _secret_key(key_text):
                out[key_text] = "<redacted>"
            else:
                out[key_text] = sanitize_observation(item)
        return out
    if isinstance(value, list):
        return [sanitize_observation(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_observation(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _secret_key(key: str) -> bool:
    normalized = key.lower()
    if normalized.endswith("_env") or normalized.endswith("_env_name"):
        return False
    return any(marker in normalized for marker in ("api_key", "password", "secret", "token", "auth"))


__all__ = ["JsonlObservationSink", "ObservationSink", "sanitize_observation"]
