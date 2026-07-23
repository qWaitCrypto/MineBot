"""Transport boundary for Body protocol calls."""

from __future__ import annotations

from typing import Protocol


class BodyTransport(Protocol):
    def request(self, command: str) -> str:
        """Send one logical Body request and return the raw response envelope."""

    def request_once(self, command: str) -> str:
        """Send one request without a transport-layer retry.

        Implementations may omit this optional capability; Body clients fall
        back to ``request`` for test doubles and transports without a retry
        policy.  The RCON implementation uses it for mutation dispatches so
        a lost response cannot cause a blind second write.
        """

    def reconnect(self) -> None:
        """Re-establish the transport after an ambiguous request."""
