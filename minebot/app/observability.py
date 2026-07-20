"""Durable observation sinks for Agent Phase 1."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol


_RAW_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<label>\b(?:api[_ -]?key|password|secret|token|auth(?:orization)?)\b\s*"
    r"(?:[:=]\s*|is\s+))"
    r"(?P<quote>['\"]?)(?P<value>[^\s,;\)\]\}>\"']{8,})(?P=quote)",
    re.IGNORECASE,
)
_BEARER_TOKEN_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}\b", re.IGNORECASE)


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
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_text(str(value))


def _sanitize_text(value: str) -> str:
    safe = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group('label')}<redacted>", value)
    safe = _BEARER_TOKEN_RE.sub("Bearer <redacted>", safe)
    for pattern in _RAW_SECRET_PATTERNS:
        safe = pattern.sub("<redacted>", safe)
    return safe


def _secret_key(key: str) -> bool:
    normalized = key.lower()
    if normalized.endswith("_env") or normalized.endswith("_env_name"):
        return False
    return any(marker in normalized for marker in ("api_key", "password", "secret", "token", "auth"))


__all__ = ["JsonlObservationSink", "ObservationSink", "sanitize_observation"]
