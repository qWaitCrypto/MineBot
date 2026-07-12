"""Pure Camera control contracts shared by probes and client adapters."""

from camera.control.follow import CameraPose, FollowConfig, FollowController
from camera.control.observer import ObserverControlClient, ObserverControlError
from camera.control.sequence import SequenceGapError, SequenceTracker

__all__ = [
    "CameraPose",
    "FollowConfig",
    "FollowController",
    "ObserverControlClient",
    "ObserverControlError",
    "SequenceGapError",
    "SequenceTracker",
]
