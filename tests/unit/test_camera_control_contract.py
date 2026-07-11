from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from camera.control.follow import CameraPose, FollowConfig, FollowController
from camera.control.sequence import SequenceGapError, SequenceTracker


def _transform(
    *,
    pos: tuple[float, float, float] = (10.0, 64.0, -3.0),
    yaw: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(pos=pos, yaw=yaw)


def test_follow_defaults_preserve_camera_branch_orbit_vector() -> None:
    controller = FollowController(FollowConfig())

    pose = controller.pose(_transform())

    horizontal = math.cos(math.radians(25.0)) * 5.0
    assert isinstance(pose, CameraPose)
    assert pose.target == pytest.approx((10.0, 65.6, -3.0))
    assert pose.eye == pytest.approx((10.0, 65.6 + math.sin(math.radians(25.0)) * 5.0, -3.0 - horizontal))
    assert pose.fov_deg == 70.0


def test_follow_preserves_yaw_relative_azimuth_and_damping() -> None:
    controller = FollowController(FollowConfig(stiffness=0.2))
    first = controller.pose(_transform(pos=(0.0, 64.0, 0.0), yaw=90.0))
    second = controller.pose(_transform(pos=(10.0, 64.0, 0.0), yaw=90.0))

    assert first.target == pytest.approx((0.0, 65.6, 0.0))
    assert second.target == pytest.approx((10.0, 65.6, 0.0))
    assert second.eye[0] == pytest.approx(first.eye[0] * 0.8 + (10.0 - math.cos(math.radians(25.0)) * 5.0) * 0.2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("distance", 0.0),
        ("distance", math.inf),
        ("azimuth_deg", math.nan),
        ("elevation_deg", 90.0),
        ("height_offset", math.inf),
        ("stiffness", -0.01),
        ("stiffness", 1.01),
        ("fov_deg", 179.0),
        ("collision_margin", -0.01),
    ],
)
def test_follow_config_rejects_non_finite_or_unsafe_values(field: str, value: float) -> None:
    values = FollowConfig().__dict__ | {field: value}
    with pytest.raises(ValueError, match=field):
        FollowConfig(**values)


def test_sequence_tracker_preserves_contiguous_sequence_contract() -> None:
    tracker = SequenceTracker(initial_seq=7)

    tracker.check({"seq": 8})
    tracker.check({"seq": 9})

    assert tracker.last_seq == 9
    with pytest.raises(SequenceGapError, match="got 11, expected 10"):
        tracker.check({"seq": 11})
    assert tracker.last_seq == 9


@pytest.mark.parametrize("seq", [None, -1, "not-an-int", True])
def test_sequence_tracker_rejects_malformed_sequence(seq: object) -> None:
    tracker = SequenceTracker()
    with pytest.raises(SequenceGapError):
        tracker.check({"seq": seq})
