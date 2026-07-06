from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from camera.model.world import SectionData
from camera.assets.vanilla import Atlas
from camera.render.mesher import Mesh, mesh_section
from camera.worldstream.protocol import SectionKey


@dataclass(frozen=True)
class MeshBuildResult:
    mesh: Mesh
    rebuilt_sections: int
    cached_sections: int


class SectionMeshCache:
    def __init__(self) -> None:
        self._meshes: dict[SectionKey, Mesh] = {}
        self._combined_signature: tuple[SectionKey, ...] | None = None
        self._combined_mesh: Mesh | None = None
        self._atlas: Atlas | None = None

    def set_atlas(self, atlas: Atlas | None) -> None:
        if atlas is self._atlas:
            return
        self._atlas = atlas
        self.clear()

    def clear(self) -> None:
        self._meshes.clear()
        self._combined_signature = None
        self._combined_mesh = None

    def mesh_visible(
        self,
        sections: dict[SectionKey, SectionData],
        dirty_sections: set[SectionKey],
        center: SectionKey,
        view_radius_chunks: int,
    ) -> MeshBuildResult:
        current_keys = set(sections)
        for removed_key in set(self._meshes) - current_keys:
            self._meshes.pop(removed_key, None)

        rebuilt = 0
        for key in dirty_sections:
            section = sections.get(key)
            if section is None:
                self._meshes.pop(key, None)
                continue
            self._meshes[key] = mesh_section(section, self._atlas)
            rebuilt += 1

        max_distance = max(0, view_radius_chunks)
        visible_keys = tuple(
            sorted(
                (
                    key
                    for key in self._meshes
                    if abs(key.x - center.x) <= max_distance and abs(key.z - center.z) <= max_distance
                ),
                key=lambda key: (key.x, key.y, key.z),
            )
        )
        visible_dirty = any(key in dirty_sections for key in visible_keys)
        if (
            self._combined_mesh is not None
            and self._combined_signature == visible_keys
            and not visible_dirty
        ):
            return MeshBuildResult(
                mesh=self._combined_mesh,
                rebuilt_sections=rebuilt,
                cached_sections=len(visible_keys),
            )

        parts: list[np.ndarray] = []
        face_count = 0
        for key in visible_keys:
            mesh = self._meshes[key]
            if mesh.face_count:
                parts.append(mesh.vertices)
                face_count += mesh.face_count
        if not parts:
            combined = Mesh(vertices=np.zeros((0, 6), dtype=np.float32), face_count=0)
        elif len(parts) == 1:
            combined = Mesh(vertices=parts[0], face_count=face_count)
        else:
            combined = Mesh(vertices=np.concatenate(parts, axis=0), face_count=face_count)
        self._combined_signature = visible_keys
        self._combined_mesh = combined
        return MeshBuildResult(mesh=combined, rebuilt_sections=rebuilt, cached_sections=len(visible_keys))
