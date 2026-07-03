"""Body protocol errors."""


class BodyProtocolError(Exception):
    """Base class for body protocol failures."""


class BodyActionTimeoutError(TimeoutError):
    """A Body action did not emit an accepted terminal event before deadline."""

    def __init__(self, message: str, *, diagnostics: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


class TruncatedPayloadError(BodyProtocolError):
    """RCON returned a payload at or above the known truncation boundary."""


class IncompletePayloadError(BodyProtocolError):
    """A protocol envelope declared itself incomplete."""


class EnvelopeError(BodyProtocolError):
    """A response did not match the required JSON envelope."""


class RconError(BodyProtocolError):
    """RCON transport failure."""
