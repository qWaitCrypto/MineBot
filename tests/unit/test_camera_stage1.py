from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from camera.model.world import SectionData, SectionStore, TransformBuffer
from camera.render.cache import SectionMeshCache
from camera.render.mesher import mesh_section, visible_face_masks
from camera.worldstream.protocol import Keyframe, SectionKey, TransformSample


CAMERA_ROOT = Path("camera")


def test_camera_package_does_not_import_agent_or_body_runtime() -> None:
    forbidden = (
        "minebot.brain",
        "minebot.app",
        "minebot.body",
        "minebot.game",
    )
    offenders: list[str] = []
    for path in CAMERA_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden):
                        offenders.append(f"{path}:{alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden):
                    offenders.append(f"{path}:{module}")
    assert offenders == []


def test_visible_face_masks_are_neighbor_shift_based() -> None:
    occupied = np.zeros((16, 16, 16), dtype=bool)
    occupied[8, 8, 8] = True
    masks = visible_face_masks(occupied)
    assert len(masks) == 6
    assert [int(mask.sum()) for mask in masks] == [1, 1, 1, 1, 1, 1]

    occupied[8, 8, 9] = True
    masks = visible_face_masks(occupied)
    assert sum(int(mask.sum()) for mask in masks) == 10


def test_mesh_section_emits_vectorized_visible_faces() -> None:
    key = SectionKey(0, 4, 0)
    indices = np.zeros((16, 16, 16), dtype=np.uint16)
    indices[8, 8, 8] = 1
    section = SectionData(key=key, palette=("minecraft:air", "minecraft:stone"), indices=indices)

    mesh = mesh_section(section)

    assert mesh.face_count == 6
    assert mesh.vertices.shape == (36, 6)
    assert np.isclose(mesh.vertices[:, 0].min(), 8.0)
    assert np.isclose(mesh.vertices[:, 0].max(), 9.0)


def test_section_mesh_cache_reuses_clean_visible_mesh() -> None:
    key = SectionKey(1, 4, 2)
    indices = np.zeros((16, 16, 16), dtype=np.uint16)
    indices[8, 8, 8] = 1
    section = SectionData(key=key, palette=("minecraft:air", "minecraft:stone"), indices=indices)
    cache = SectionMeshCache()

    first = cache.mesh_visible({key: section}, {key}, key, view_radius_chunks=4)
    second = cache.mesh_visible({key: section}, set(), key, view_radius_chunks=4)

    assert first.rebuilt_sections == 1
    assert first.mesh.face_count == 6
    assert second.rebuilt_sections == 0
    assert second.mesh is first.mesh


def test_world_model_and_mesh_cache_clear_on_reconnect() -> None:
    key = SectionKey(1, 4, 2)
    keyframe = Keyframe(
        sub_id="camera-follow",
        dimension="minecraft:overworld",
        section=key,
        palette=("minecraft:air", "minecraft:stone"),
        indices=[0] * 4096,
        encoding="json-array-debug-u16",
    )
    store = SectionStore()
    store.apply_keyframe(keyframe)
    assert store.sections
    assert store.dirty_sections
    store.clear()
    assert store.sections == {}
    assert store.dirty_sections == set()

    transforms = TransformBuffer()
    transforms.add(
        TransformSample(
            entity="Bot1",
            dimension="minecraft:overworld",
            pos=(0.0, 64.0, 0.0),
            yaw=0.0,
            pitch=0.0,
            on_ground=True,
            pose="standing",
            monotonic_s=1.0,
        )
    )
    assert transforms.latest() is not None
    transforms.clear()
    assert transforms.latest() is None

    cache = SectionMeshCache()
    section = SectionData(key=key, palette=("minecraft:air", "minecraft:stone"), indices=np.ones((16, 16, 16), dtype=np.uint16))
    assert cache.mesh_visible({key: section}, {key}, key, view_radius_chunks=4).mesh.face_count > 0
    cache.clear()
    rebuilt = cache.mesh_visible({key: section}, {key}, key, view_radius_chunks=4)
    assert rebuilt.rebuilt_sections == 1
