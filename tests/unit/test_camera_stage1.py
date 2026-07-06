from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from camera.assets.vanilla import (
    MISSING_TEXTURE,
    block_id,
    build_atlas,
    is_occluding_cube_state,
    is_renderable_cube_state,
    load_blockstate_model,
    resolve_client_jar,
)
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


def test_visible_face_masks_follow_yzx_section_axes() -> None:
    occupied = np.zeros((16, 16, 16), dtype=bool)
    occupied[8, 8, 8] = True

    occupied[9, 8, 8] = True
    masks = visible_face_masks(occupied)
    assert not masks[2][8, 8, 8]
    assert not masks[3][9, 8, 8]
    assert masks[0][8, 8, 8]
    assert masks[4][8, 8, 8]


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
    assert np.all(mesh.vertices[:, 3:5] >= 0.0)
    assert np.all(mesh.vertices[:, 3:5] <= 1.0)
    assert np.all(mesh.vertices[:, 5] > 0.0)


def test_mesh_section_uses_per_corner_ao() -> None:
    key = SectionKey(0, 4, 0)
    indices = np.zeros((16, 16, 16), dtype=np.uint16)
    indices[8, 8, 8] = 1
    indices[8, 9, 8] = 1
    indices[8, 8, 7] = 1
    section = SectionData(key=key, palette=("minecraft:air", "minecraft:stone"), indices=indices)

    mesh = mesh_section(section)
    faces = mesh.vertices.reshape((-1, 6, 6))
    top_faces = [
        face
        for face in faces
        if np.allclose(face[:, 1], 73.0)
        and np.isclose(face[:, 0].min(), 8.0)
        and np.isclose(face[:, 0].max(), 9.0)
        and np.isclose(face[:, 2].min(), 8.0)
        and np.isclose(face[:, 2].max(), 9.0)
    ]

    assert len(top_faces) == 1
    assert np.unique(np.round(top_faces[0][:, 5], 4)).size > 1


def test_water_renders_as_non_occluding_top_surface() -> None:
    key = SectionKey(0, 4, 0)
    indices = np.zeros((16, 16, 16), dtype=np.uint16)
    indices[8, 8, 8] = 1
    indices[8, 8, 9] = 2
    indices[8, 8, 10] = 2
    section = SectionData(key=key, palette=("minecraft:air", "minecraft:stone", "minecraft:water"), indices=indices)

    mesh = mesh_section(section)

    assert is_renderable_cube_state("minecraft:water")
    assert not is_occluding_cube_state("minecraft:water")
    assert mesh.face_count == 8


def test_mesh_section_omits_non_cube_plants_without_placeholder_cube() -> None:
    key = SectionKey(0, 4, 0)
    indices = np.ones((16, 16, 16), dtype=np.uint16)
    section = SectionData(key=key, palette=("minecraft:air", "minecraft:short_grass"), indices=indices)

    mesh = mesh_section(section)

    assert not is_renderable_cube_state("minecraft:short_grass")
    assert mesh.face_count == 0


def test_common_fidelity_spike_non_cubes_are_omitted() -> None:
    for state in (
        "minecraft:wildflowers",
        "minecraft:leaf_litter",
        "minecraft:pointed_dripstone",
        "minecraft:bush",
        "minecraft:peony",
        "minecraft:seagrass",
        "minecraft:tall_seagrass",
        "minecraft:rose_bush",
        "minecraft:small_amethyst_bud",
        "minecraft:medium_amethyst_bud",
        "minecraft:large_amethyst_bud",
        "minecraft:amethyst_cluster",
        "minecraft:chest",
    ):
        assert not is_renderable_cube_state(state)


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


def test_vanilla_asset_atlas_uses_local_client_jar() -> None:
    client_jar = resolve_client_jar()
    atlas = build_atlas(client_jar)
    stone_model = load_blockstate_model(client_jar, "minecraft:stone")
    grass_model = load_blockstate_model(client_jar, "minecraft:grass_block")
    oak_log_model = load_blockstate_model(client_jar, "minecraft:oak_log")
    sandstone_model = load_blockstate_model(client_jar, "minecraft:sandstone")

    assert atlas.client_jar.exists()
    assert atlas.atlas_path.exists()
    assert "block/stone" in atlas.texture_uvs
    assert "block/grass_block_top" in atlas.texture_uvs
    assert "block/raw_copper_block" in atlas.texture_uvs
    assert "block/smooth_basalt" in atlas.texture_uvs
    assert "block/amethyst_block" in atlas.texture_uvs
    assert "block/spawner" in atlas.texture_uvs
    assert "block/pumpkin_side" in atlas.texture_uvs
    assert MISSING_TEXTURE in atlas.texture_uvs
    assert block_id("minecraft:stone[foo=bar]") == "minecraft:stone"
    assert stone_model is not None
    assert set(stone_model.values()) == {"block/stone"}
    assert grass_model is not None
    assert grass_model["up"] == "block/grass_block_top"
    assert grass_model["down"] == "block/dirt"
    assert grass_model["north"] == "block/grass_block_side_overlay"
    assert atlas.materials["minecraft:grass_block"]["north"] == "block/grass_block_side"
    assert oak_log_model is not None
    assert oak_log_model["north"] == "block/oak_log"
    assert oak_log_model["up"] == "block/oak_log_top"
    assert atlas.texture_for_state("minecraft:oak_log[axis=y]", 2) == "block/oak_log_top"
    assert sandstone_model is not None
    assert sandstone_model["up"] == "block/sandstone_top"
    assert sandstone_model["down"] == "block/sandstone_bottom"
    assert atlas.texture_for_state("minecraft:bubble_column[drag=true]", 2) == "block/water_still"
