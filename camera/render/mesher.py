from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from camera.model.world import SectionData
from camera.worldstream.protocol import SectionKey


FACE_NORMALS = np.asarray(
    [
        [1.0, 0.0, 0.0],
        [-1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, -1.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, 0.0, -1.0],
    ],
    dtype=np.float32,
)

FACE_SHADES = np.asarray([0.72, 0.64, 1.0, 0.46, 0.82, 0.58], dtype=np.float32)

FACE_VERTEX_OFFSETS = np.asarray(
    [
        [[1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [1.0, 1.0, 1.0], [1.0, 0.0, 0.0], [1.0, 1.0, 1.0], [1.0, 0.0, 1.0]],
        [[0.0, 0.0, 1.0], [0.0, 1.0, 1.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]],
        [[0.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 0.0], [0.0, 1.0, 1.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 0.0, 1.0], [0.0, 0.0, 0.0], [1.0, 0.0, 1.0], [0.0, 0.0, 1.0]],
        [[1.0, 0.0, 1.0], [1.0, 1.0, 1.0], [0.0, 1.0, 1.0], [1.0, 0.0, 1.0], [0.0, 1.0, 1.0], [0.0, 0.0, 1.0]],
        [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 1.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
    ],
    dtype=np.float32,
)


@dataclass(frozen=True)
class Mesh:
    vertices: np.ndarray
    face_count: int


def visible_face_masks(occupied: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    padded = np.pad(occupied, 1, mode="constant", constant_values=False)
    center = padded[1:-1, 1:-1, 1:-1]
    xp = center & ~padded[2:, 1:-1, 1:-1]
    xm = center & ~padded[:-2, 1:-1, 1:-1]
    yp = center & ~padded[1:-1, 2:, 1:-1]
    ym = center & ~padded[1:-1, :-2, 1:-1]
    zp = center & ~padded[1:-1, 1:-1, 2:]
    zm = center & ~padded[1:-1, 1:-1, :-2]
    return xp, xm, yp, ym, zp, zm


def mesh_section(section: SectionData) -> Mesh:
    occupied = section.occupied_mask()
    masks = visible_face_masks(occupied)
    parts: list[np.ndarray] = []
    base = np.asarray((section.key.x * 16.0, section.key.y * 16.0, section.key.z * 16.0), dtype=np.float32)
    for face_index, mask in enumerate(masks):
        coords = np.argwhere(mask)
        if coords.size:
            parts.append(_face_vertices(coords, base, face_index))
    if not parts:
        return Mesh(vertices=np.zeros((0, 6), dtype=np.float32), face_count=0)
    vertices = np.concatenate(parts, axis=0)
    return Mesh(vertices=vertices, face_count=int(vertices.shape[0] // 6))


def mesh_sections(sections: list[SectionData], center: SectionKey, view_radius_chunks: int) -> Mesh:
    parts: list[np.ndarray] = []
    face_count = 0
    max_distance = max(0, view_radius_chunks)
    for section in sections:
        if abs(section.key.x - center.x) > max_distance or abs(section.key.z - center.z) > max_distance:
            continue
        mesh = mesh_section(section)
        if mesh.face_count:
            parts.append(mesh.vertices)
            face_count += mesh.face_count
    if not parts:
        return Mesh(vertices=np.zeros((0, 6), dtype=np.float32), face_count=0)
    return Mesh(vertices=np.concatenate(parts, axis=0), face_count=face_count)


def _face_vertices(coords_yzx: np.ndarray, section_base: np.ndarray, face: int) -> np.ndarray:
    origins = coords_yzx[:, [2, 0, 1]].astype(np.float32, copy=False) + section_base
    positions = origins[:, np.newaxis, :] + FACE_VERTEX_OFFSETS[face][np.newaxis, :, :]
    color = np.asarray(_face_color(face, float(FACE_SHADES[face])), dtype=np.float32)
    colors = np.broadcast_to(color, positions.shape)
    return np.concatenate((positions, colors), axis=2).reshape((-1, 6))


def _face_color(face: int, shade: float) -> tuple[float, float, float]:
    # Placeholder block material: green top, brown bottom, muted grass sides.
    if face == 2:
        base = (0.35, 0.72, 0.32)
    elif face == 3:
        base = (0.38, 0.27, 0.18)
    else:
        base = (0.52, 0.47, 0.34)
    return tuple(component * shade for component in base)
