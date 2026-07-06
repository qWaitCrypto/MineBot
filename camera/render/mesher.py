from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from camera.model.world import SectionData
from camera.assets.vanilla import Atlas, MISSING_TEXTURE, is_fluid_state, is_occluding_cube_state, is_renderable_cube_state
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

FACE_SHADES = np.asarray([0.78, 0.70, 1.0, 0.50, 0.86, 0.64], dtype=np.float32)

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

FACE_AO_AXES_YZX = (
    (0, 1),  # +x: y/z
    (0, 1),  # -x: y/z
    (1, 2),  # +y: z/x
    (1, 2),  # -y: z/x
    (0, 2),  # +z: y/x
    (0, 2),  # -z: y/x
)
AO_LEVELS = np.asarray([1.0, 0.82, 0.68, 0.54], dtype=np.float32)


@dataclass(frozen=True)
class Mesh:
    vertices: np.ndarray
    face_count: int


def visible_face_masks(
    renderable: np.ndarray,
    occluding: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if occluding is None:
        occluding = renderable
    padded_renderable = np.pad(renderable, 1, mode="constant", constant_values=False)
    padded_occluding = np.pad(occluding, 1, mode="constant", constant_values=False)
    center = padded_renderable[1:-1, 1:-1, 1:-1]
    xp = center & ~padded_occluding[1:-1, 1:-1, 2:]
    xm = center & ~padded_occluding[1:-1, 1:-1, :-2]
    yp = center & ~padded_occluding[2:, 1:-1, 1:-1]
    ym = center & ~padded_occluding[:-2, 1:-1, 1:-1]
    zp = center & ~padded_occluding[1:-1, 2:, 1:-1]
    zm = center & ~padded_occluding[1:-1, :-2, 1:-1]
    return xp, xm, yp, ym, zp, zm


def mesh_section(section: SectionData, atlas: Atlas | None = None) -> Mesh:
    renderable = _renderable_cube_mask(section)
    fluid = _fluid_cube_mask(section)
    solid = renderable & ~fluid
    occluding = _occluding_cube_mask(section)
    masks = visible_face_masks(solid, occluding)
    fluid_masks = list(visible_face_masks(fluid, occluding | fluid))
    for face_index in (0, 1, 3, 4, 5):
        fluid_masks[face_index] = np.zeros_like(fluid_masks[face_index], dtype=bool)
    parts: list[np.ndarray] = []
    base = np.asarray((section.key.x * 16.0, section.key.y * 16.0, section.key.z * 16.0), dtype=np.float32)
    for face_index, mask in enumerate((*masks, *fluid_masks)):
        material_face = face_index % 6
        coords = np.argwhere(mask)
        if coords.size:
            parts.append(_face_vertices(section, coords, base, material_face, occluding, atlas))
    if not parts:
        return Mesh(vertices=np.zeros((0, 6), dtype=np.float32), face_count=0)
    vertices = np.concatenate(parts, axis=0)
    return Mesh(vertices=vertices, face_count=int(vertices.shape[0] // 6))


def _renderable_cube_mask(section: SectionData) -> np.ndarray:
    mask = np.zeros_like(section.indices, dtype=bool)
    for palette_index, state in enumerate(section.palette):
        if is_renderable_cube_state(state):
            mask |= section.indices == palette_index
    return mask


def _occluding_cube_mask(section: SectionData) -> np.ndarray:
    mask = np.zeros_like(section.indices, dtype=bool)
    for palette_index, state in enumerate(section.palette):
        if is_occluding_cube_state(state):
            mask |= section.indices == palette_index
    return mask


def _fluid_cube_mask(section: SectionData) -> np.ndarray:
    mask = np.zeros_like(section.indices, dtype=bool)
    for palette_index, state in enumerate(section.palette):
        if is_fluid_state(state):
            mask |= section.indices == palette_index
    return mask


def mesh_sections(sections: list[SectionData], center: SectionKey, view_radius_chunks: int, atlas: Atlas | None = None) -> Mesh:
    parts: list[np.ndarray] = []
    face_count = 0
    max_distance = max(0, view_radius_chunks)
    for section in sections:
        if abs(section.key.x - center.x) > max_distance or abs(section.key.z - center.z) > max_distance:
            continue
        mesh = mesh_section(section, atlas)
        if mesh.face_count:
            parts.append(mesh.vertices)
            face_count += mesh.face_count
    if not parts:
        return Mesh(vertices=np.zeros((0, 6), dtype=np.float32), face_count=0)
    return Mesh(vertices=np.concatenate(parts, axis=0), face_count=face_count)


def _face_vertices(
    section: SectionData,
    coords_yzx: np.ndarray,
    section_base: np.ndarray,
    face: int,
    occupied: np.ndarray,
    atlas: Atlas | None,
) -> np.ndarray:
    origins = coords_yzx[:, [2, 0, 1]].astype(np.float32, copy=False) + section_base
    positions = origins[:, np.newaxis, :] + FACE_VERTEX_OFFSETS[face][np.newaxis, :, :]
    uvs = _face_uvs(section, coords_yzx, face, atlas)
    shade = (_vertex_ao(occupied, coords_yzx, face) * FACE_SHADES[face]).astype(np.float32)
    return np.concatenate((positions, uvs, shade[:, :, np.newaxis]), axis=2).reshape((-1, 6))


def _face_uvs(section: SectionData, coords_yzx: np.ndarray, face: int, atlas: Atlas | None) -> np.ndarray:
    if atlas is None:
        uv_rect = (0.0, 0.0, 1.0, 1.0)
        texture_for_state = None
    else:
        texture_for_state = atlas.uv_for_state
    indices = section.indices[coords_yzx[:, 0], coords_yzx[:, 1], coords_yzx[:, 2]]
    uv_parts: list[np.ndarray] = []
    for palette_index in np.unique(indices):
        mask = indices == palette_index
        state = section.palette[int(palette_index)] if int(palette_index) < len(section.palette) else MISSING_TEXTURE
        uv_rect = texture_for_state(state, face) if texture_for_state is not None else uv_rect
        uv_parts.append(_uv_template(uv_rect, int(mask.sum())))
    if len(uv_parts) == 1:
        return uv_parts[0]
    out = np.zeros((coords_yzx.shape[0], 6, 2), dtype=np.float32)
    cursor = 0
    for palette_index in np.unique(indices):
        mask = indices == palette_index
        count = int(mask.sum())
        out[mask] = uv_parts[cursor]
        cursor += 1
    return out


def _uv_template(uv_rect: tuple[float, float, float, float], count: int) -> np.ndarray:
    u0, v0, u1, v1 = uv_rect
    # Flip V because PIL stores top-left origin while OpenGL samples bottom-left.
    template = np.asarray(
        [
            [u0, v1],
            [u0, v0],
            [u1, v0],
            [u0, v1],
            [u1, v0],
            [u1, v1],
        ],
        dtype=np.float32,
    )
    return np.broadcast_to(template, (count, 6, 2)).copy()


def _vertex_ao(occupied: np.ndarray, coords_yzx: np.ndarray, face: int) -> np.ndarray:
    padded = np.pad(occupied, 1, mode="constant", constant_values=False)
    base = coords_yzx + 1
    local_yzx = FACE_VERTEX_OFFSETS[face][:, [1, 2, 0]]
    axis_a, axis_b = FACE_AO_AXES_YZX[face]
    shades = np.empty((coords_yzx.shape[0], 6), dtype=np.float32)
    for vertex_index in range(6):
        offset_a = np.zeros(3, dtype=np.int8)
        offset_b = np.zeros(3, dtype=np.int8)
        offset_a[axis_a] = 1 if local_yzx[vertex_index, axis_a] > 0.5 else -1
        offset_b[axis_b] = 1 if local_yzx[vertex_index, axis_b] > 0.5 else -1
        side_a = _sample_padded(padded, base + offset_a)
        side_b = _sample_padded(padded, base + offset_b)
        corner = _sample_padded(padded, base + offset_a + offset_b)
        occlusion = np.where(side_a & side_b, 3, side_a.astype(np.uint8) + side_b.astype(np.uint8) + corner.astype(np.uint8))
        shades[:, vertex_index] = AO_LEVELS[occlusion]
    if face == 2:
        shades = np.maximum(shades, 0.82)
    return shades


def _sample_padded(padded: np.ndarray, coords_yzx: np.ndarray) -> np.ndarray:
    return padded[coords_yzx[:, 0], coords_yzx[:, 1], coords_yzx[:, 2]]
