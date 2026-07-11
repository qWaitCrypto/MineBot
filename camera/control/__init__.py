"""Pure Camera control contracts shared by probes and client adapters."""

from camera.control.follow import CameraPose, FollowConfig, FollowController
from camera.control.sequence import SequenceGapError, SequenceTracker

__all__ = [
    "CameraPose",
    "FollowConfig",
    "FollowController",
    "SequenceGapError",
    "SequenceTracker",
]
