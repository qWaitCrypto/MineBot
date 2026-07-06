from __future__ import annotations

import math
from dataclasses import dataclass

from camera.worldstream.protocol import TransformSample


@dataclass(frozen=True)
class CameraPose:
    eye: tuple[float, float, float]
    target: tuple[float, float, float]
    fov_deg: float


@dataclass(frozen=True)
class FollowConfig:
    distance: float = 5.0
    azimuth_deg: float = 180.0
    elevation_deg: float = 25.0
    height_offset: float = 1.6
    stiffness: float = 0.2
    fov_deg: float = 70.0


class FollowController:
    def __init__(self, config: FollowConfig) -> None:
        self.config = config
        self._last_eye: tuple[float, float, float] | None = None

    def pose(self, transform: TransformSample) -> CameraPose:
        target = (
            transform.pos[0],
            transform.pos[1] + self.config.height_offset,
            transform.pos[2],
        )
        yaw_rad = math.radians(transform.yaw + self.config.azimuth_deg)
        elevation_rad = math.radians(self.config.elevation_deg)
        horizontal = math.cos(elevation_rad) * self.config.distance
        desired_eye = (
            target[0] + math.sin(yaw_rad) * horizontal,
            target[1] + math.sin(elevation_rad) * self.config.distance,
            target[2] + math.cos(yaw_rad) * horizontal,
        )
        if self._last_eye is None:
            eye = desired_eye
        else:
            alpha = max(0.0, min(1.0, self.config.stiffness))
            eye = tuple(self._last_eye[index] * (1.0 - alpha) + desired_eye[index] * alpha for index in range(3))
        self._last_eye = eye
        return CameraPose(eye=eye, target=target, fov_deg=self.config.fov_deg)
