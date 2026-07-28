"""Shared sanitization for player-visible Body chat egress."""

from __future__ import annotations

import re


CHAT_TEXT_LIMIT = 220
_MINECRAFT_FORMATTING_RE = re.compile(r"§.")
_NUMBER_PATTERN = r"-?\d+(?:\.\d+)?"
_COORDINATE_TRIPLE_RE = re.compile(
    rf"(?<![\w.-])[\[(]\s*{_NUMBER_PATTERN}\s*[,，]\s*{_NUMBER_PATTERN}\s*[,，]\s*{_NUMBER_PATTERN}\s*[\])]"
)
_BARE_COORDINATE_TRIPLE_RE = re.compile(
    rf"(?<![\w.-]){_NUMBER_PATTERN}\s*[,，]\s*{_NUMBER_PATTERN}\s*[,，]\s*{_NUMBER_PATTERN}(?![\w-])"
)
_AXIS_COORDINATE_TRIPLE_RE = re.compile(
    rf"(?i)(?<!\w)x\s*[:=]?\s*{_NUMBER_PATTERN}\s*[,，;；]?\s*"
    rf"y\s*[:=]?\s*{_NUMBER_PATTERN}\s*[,，;；]?\s*"
    rf"z\s*[:=]?\s*{_NUMBER_PATTERN}(?!\w)"
)
_LABELED_SPACE_COORDINATE_TRIPLE_RE = re.compile(
    rf"(?i)(?<!\w)(?:coordinates?|coords?|position|pos|located\s+at|at|坐标|位置|位于)"
    rf"\s*(?:is|are|about|around|roughly|approximately|为|是|在|约|大约|大概)?\s*[:：=]?\s*"
    rf"{_NUMBER_PATTERN}\s+{_NUMBER_PATTERN}\s+{_NUMBER_PATTERN}(?![\w-])"
)


def sanitize_chat_text(text: str) -> str:
    cleaned = str(text).replace("\r", " ").replace("\n", " ")
    cleaned = _MINECRAFT_FORMATTING_RE.sub("", cleaned)
    cleaned = _COORDINATE_TRIPLE_RE.sub("[position]", cleaned)
    cleaned = _BARE_COORDINATE_TRIPLE_RE.sub("[position]", cleaned)
    cleaned = _AXIS_COORDINATE_TRIPLE_RE.sub("[position]", cleaned)
    cleaned = _LABELED_SPACE_COORDINATE_TRIPLE_RE.sub("[position]", cleaned)
    cleaned = "".join(ch for ch in cleaned if ch == "\t" or ord(ch) >= 32)
    cleaned = " ".join(cleaned.strip().split())
    return cleaned[:CHAT_TEXT_LIMIT]


__all__ = ["CHAT_TEXT_LIMIT", "sanitize_chat_text"]
