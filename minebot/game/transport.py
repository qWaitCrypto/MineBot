"""Transport boundary for Body protocol calls."""

from __future__ import annotations

from typing import Protocol


class BodyTransport(Protocol):
    def request(self, command: str) -> str:
        """Send one logical Body request and return the raw response envelope."""
