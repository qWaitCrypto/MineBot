"""Body protocol errors."""


class BodyProtocolError(Exception):
    """Base class for body protocol failures."""


class TruncatedPayloadError(BodyProtocolError):
    """RCON returned a payload at or above the known truncation boundary."""


class IncompletePayloadError(BodyProtocolError):
    """A protocol envelope declared itself incomplete."""


class EnvelopeError(BodyProtocolError):
    """A response did not match the required JSON envelope."""


class RconError(BodyProtocolError):
    """RCON transport failure."""
