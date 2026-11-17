"""Deterministic horizontal geometry for observed thrown-entity rays."""

from __future__ import annotations

from math import hypot
from typing import Any, Mapping


def intersect_thrown_entity_rays(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, object]:
    """Intersect two forward rays encoded by throw_position and direction [x, z]."""
    p1, p2 = first["throw_position"], second["throw_position"]
    d1, d2 = first["direction"], second["direction"]
    x1, z1 = float(p1[0]), float(p1[2])
    x2, z2 = float(p2[0]), float(p2[2])
    dx1, dz1 = float(d1[0]), float(d1[1])
    dx2, dz2 = float(d2[0]), float(d2[1])
    cross = dx1 * dz2 - dz1 * dx2
    if abs(cross) < 1e-6:
        return {"status": "near_parallel"}
    qx, qz = x2 - x1, z2 - z1
    along_first = (qx * dz2 - qz * dx2) / cross
    along_second = (qx * dz1 - qz * dx1) / cross
    if along_first < 0 or along_second < 0:
        return {"status": "behind"}
    x, z = x1 + along_first * dx1, z1 + along_first * dz1
    distance = hypot(x - x2, z - z2)
    return {
        "status": "intersection",
        "position": [x, z],
        "distance_from_second": distance,
        "fact": f"The two throws cross at x={x:.1f}, z={z:.1f}, about {distance:.0f} blocks from here.",
    }
