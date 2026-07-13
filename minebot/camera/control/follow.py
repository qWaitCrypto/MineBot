from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


class TransformLike(Protocol):
    pos: tuple[float, float, float]
    yaw: float


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
    collision_margin: float = 0.25

    def __post_init__(self) -> None:
        values = {
            "distance": self.distance,
            "azimuth_deg": self.azimuth_deg,
            "elevation_deg": self.elevation_deg,
            "height_offset": self.height_offset,
            "stiffness": self.stiffness,
            "fov_deg": self.fov_deg,
            "collision_margin": self.collision_margin,
        }
        for field, value in values.items():
            if not math.isfinite(value):
                raise ValueError(f"{field} must be finite")
        if not 0.1 <= self.distance <= 64.0:
            raise ValueError("distance must be between 0.1 and 64.0")
        if not -89.0 <= self.elevation_deg <= 89.0:
            raise ValueError("elevation_deg must be between -89.0 and 89.0")
        if not -64.0 <= self.height_offset <= 64.0:
            raise ValueError("height_offset must be between -64.0 and 64.0")
        if not 0.0 <= self.stiffness <= 1.0:
            raise ValueError("stiffness must be between 0.0 and 1.0")
        if not 1.0 <= self.fov_deg <= 170.0:
            raise ValueError("fov_deg must be between 1.0 and 170.0")
        if not 0.0 <= self.collision_margin <= 8.0:
            raise ValueError("collision_margin must be between 0.0 and 8.0")


class FollowController:
    """Reference orbit math; production clients execute equivalent math per frame."""

    def __init__(self, config: FollowConfig) -> None:
        self.config = config
        self._last_eye: tuple[float, float, float] | None = None

    def pose(self, transform: TransformLike) -> CameraPose:
        if len(transform.pos) != 3 or not all(math.isfinite(value) for value in transform.pos):
            raise ValueError("transform.pos must contain three finite values")
        if not math.isfinite(transform.yaw):
            raise ValueError("transform.yaw must be finite")

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
            alpha = self.config.stiffness
            eye = tuple(
                self._last_eye[index] * (1.0 - alpha) + desired_eye[index] * alpha
                for index in range(3)
            )
        self._last_eye = eye
        return CameraPose(eye=eye, target=target, fov_deg=self.config.fov_deg)
