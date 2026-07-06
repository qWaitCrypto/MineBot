from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw


CLIENT_JAR_CANDIDATES = (
    Path("body-mod/.gradle/loom-cache/minecraftMaven/net/minecraft/minecraft-clientOnly-043a8b3edf/26.1.2/minecraft-clientOnly-043a8b3edf-26.1.2.jar"),
    Path.home() / ".minecraft/versions/26.1.2/26.1.2.jar",
)
DEFAULT_CACHE_DIR = Path(".camera-assets/26.1.2")
MISSING_TEXTURE = "minebot:missing"
GRASS_TINT = np.asarray((0.54, 0.82, 0.32, 1.0), dtype=np.float32)
LEAF_TINT = np.asarray((0.42, 0.72, 0.26, 1.0), dtype=np.float32)
WATER_TINT = np.asarray((0.22, 0.45, 1.0, 0.72), dtype=np.float32)


FACE_TO_MODEL_FACE = {
    0: "east",
    1: "west",
    2: "up",
    3: "down",
    4: "south",
    5: "north",
}


@dataclass(frozen=True)
class Atlas:
    image: Image.Image
    texture_uvs: dict[str, tuple[float, float, float, float]]
    texture_size: int
    atlas_path: Path
    client_jar: Path
    covered_blocks: tuple[str, ...]
    approximate_blocks: tuple[str, ...]
    missing_blocks: tuple[str, ...]

    def texture_for_state(self, state: str, face: int) -> str:
        block_name = block_id(state)
        material = material_for_block(block_name)
        face_name = FACE_TO_MODEL_FACE[face]
        return material.get(face_name, material.get("all", MISSING_TEXTURE))

    def uv_for_state(self, state: str, face: int) -> tuple[float, float, float, float]:
        return self.texture_uvs.get(self.texture_for_state(state, face), self.texture_uvs[MISSING_TEXTURE])

    def image_rgba_u8(self) -> np.ndarray:
        return np.asarray(self.image.convert("RGBA"), dtype=np.uint8)


def block_id(state: str) -> str:
    return state.split("[", 1)[0]


def material_for_block(block_name: str) -> dict[str, str]:
    return _MATERIALS.get(block_name, _material_from_name(block_name))


def resolve_client_jar(explicit: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(CLIENT_JAR_CANDIDATES)
    candidates.extend(Path("/mnt/c/Users/qwait/AppData/Roaming/.minecraft/versions").glob("*/26.1.2.jar"))
    for candidate in candidates:
        if candidate.exists() and _has_required_assets(candidate):
            return candidate
    raise FileNotFoundError(
        "no 26.1.2 client jar with vanilla assets found; expected assets/minecraft/{textures,blockstates,models}"
    )


def build_atlas(client_jar: Path, cache_dir: Path = DEFAULT_CACHE_DIR, tile_size: int = 16) -> Atlas:
    cache_dir.mkdir(parents=True, exist_ok=True)
    textures = sorted(required_textures(client_jar))
    if MISSING_TEXTURE not in textures:
        textures.append(MISSING_TEXTURE)
    cols = 16
    rows = max(1, (len(textures) + cols - 1) // cols)
    image = Image.new("RGBA", (cols * tile_size, rows * tile_size), (255, 0, 255, 255))
    uvs: dict[str, tuple[float, float, float, float]] = {}
    with zipfile.ZipFile(client_jar) as jar:
        for index, texture_name in enumerate(textures):
            x = (index % cols) * tile_size
            y = (index // cols) * tile_size
            if texture_name == MISSING_TEXTURE:
                tile = _missing_tile(tile_size)
            else:
                tile = _load_texture(jar, texture_name, tile_size)
                tile = _apply_tint(texture_name, tile)
            image.alpha_composite(tile, (x, y))
            uvs[texture_name] = (
                x / image.width,
                y / image.height,
                (x + tile_size) / image.width,
                (y + tile_size) / image.height,
            )
    atlas_path = cache_dir / "block-atlas.png"
    image.save(atlas_path)
    return Atlas(
        image=image,
        texture_uvs=uvs,
        texture_size=tile_size,
        atlas_path=atlas_path,
        client_jar=client_jar,
        covered_blocks=tuple(sorted(_MATERIALS)),
        approximate_blocks=tuple(sorted(_APPROXIMATE_BLOCKS)),
        missing_blocks=tuple(sorted(_MISSING_BLOCKS)),
    )


def required_textures(client_jar: Path) -> set[str]:
    with zipfile.ZipFile(client_jar) as jar:
        available = {
            path.removeprefix("assets/minecraft/textures/").removesuffix(".png")
            for path in jar.namelist()
            if path.startswith("assets/minecraft/textures/block/") and path.endswith(".png")
        }
        textures = set()
        for material in _MATERIALS.values():
            textures.update(material.values())
        for block_name in _AUTO_CUBE_BLOCKS:
            texture_name = _auto_texture_name(block_name)
            if texture_name in available:
                textures.add(texture_name)
        return textures


def load_blockstate_model(client_jar: Path, block_name: str) -> dict[str, str] | None:
    block_path = f"assets/minecraft/blockstates/{block_name.removeprefix('minecraft:')}.json"
    with zipfile.ZipFile(client_jar) as jar:
        try:
            blockstate = json.loads(jar.read(block_path))
        except KeyError:
            return None
        model_ref = _first_model_ref(blockstate)
        if not model_ref:
            return None
        return _resolve_model_textures(jar, model_ref)


def _has_required_assets(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
        return (
            "assets/minecraft/textures/block/stone.png" in names
            and "assets/minecraft/blockstates/grass_block.json" in names
            and "assets/minecraft/models/block/grass_block.json" in names
        )
    except (OSError, zipfile.BadZipFile):
        return False


def _load_texture(jar: zipfile.ZipFile, texture_name: str, tile_size: int) -> Image.Image:
    path = f"assets/minecraft/textures/{texture_name}.png"
    try:
        image = Image.open(jar.open(path)).convert("RGBA")
    except KeyError:
        return _missing_tile(tile_size)
    if image.height > image.width:
        image = image.crop((0, 0, image.width, image.width))
    if image.size != (tile_size, tile_size):
        image = image.resize((tile_size, tile_size), Image.Resampling.NEAREST)
    return image


def _apply_tint(texture_name: str, image: Image.Image) -> Image.Image:
    array = np.asarray(image, dtype=np.float32) / 255.0
    if texture_name in {"block/grass_block_top", "block/grass_block_side_overlay"}:
        array *= GRASS_TINT
    elif texture_name.endswith("_leaves") or texture_name in {"block/oak_leaves", "block/birch_leaves", "block/spruce_leaves", "block/jungle_leaves", "block/acacia_leaves", "block/dark_oak_leaves", "block/mangrove_leaves", "block/cherry_leaves", "block/azalea_leaves", "block/flowering_azalea_leaves"}:
        array *= LEAF_TINT
    elif texture_name in {"block/water_still", "block/water_flow"}:
        array *= WATER_TINT
    return Image.fromarray(np.clip(array * 255.0, 0, 255).astype(np.uint8), "RGBA")


def _missing_tile(tile_size: int) -> Image.Image:
    image = Image.new("RGBA", (tile_size, tile_size), (255, 0, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, tile_size // 2 - 1, tile_size // 2 - 1), fill=(0, 0, 0, 255))
    draw.rectangle((tile_size // 2, tile_size // 2, tile_size - 1, tile_size - 1), fill=(0, 0, 0, 255))
    return image


def _first_model_ref(blockstate: dict[str, Any]) -> str | None:
    variants = blockstate.get("variants")
    if isinstance(variants, dict) and variants:
        first_value = next(iter(variants.values()))
        if isinstance(first_value, list) and first_value:
            first_value = first_value[0]
        if isinstance(first_value, dict):
            model = first_value.get("model")
            return str(model) if model else None
    multipart = blockstate.get("multipart")
    if isinstance(multipart, list) and multipart:
        apply = multipart[0].get("apply") if isinstance(multipart[0], dict) else None
        if isinstance(apply, dict):
            model = apply.get("model")
            return str(model) if model else None
    return None


def _resolve_model_textures(jar: zipfile.ZipFile, model_ref: str) -> dict[str, str] | None:
    model_name = model_ref.removeprefix("minecraft:").removeprefix("block/")
    path = f"assets/minecraft/models/block/{model_name}.json"
    try:
        model = json.loads(jar.read(path))
    except KeyError:
        return None
    textures: dict[str, str] = {}
    parent_ref = model.get("parent")
    if isinstance(parent_ref, str):
        parent = _resolve_model_textures(jar, parent_ref)
        if parent:
            textures.update(parent)
    raw_textures = model.get("textures", {})
    if isinstance(raw_textures, dict):
        for key, value in raw_textures.items():
            if isinstance(value, str):
                textures[key] = _resolve_texture_ref(value, textures)
    face_textures: dict[str, str] = {}
    elements = model.get("elements")
    if isinstance(elements, list):
        for element in elements:
            faces = element.get("faces") if isinstance(element, dict) else None
            if not isinstance(faces, dict):
                continue
            for face_name, face_data in faces.items():
                if isinstance(face_data, dict) and isinstance(face_data.get("texture"), str):
                    face_textures[face_name] = _resolve_texture_ref(face_data["texture"], textures)
    if not face_textures:
        for face in ("north", "south", "east", "west", "up", "down"):
            face_textures[face] = textures.get(face) or textures.get("side") or textures.get("all") or textures.get("top") or textures.get("particle", MISSING_TEXTURE)
    return face_textures


def _resolve_texture_ref(value: str, textures: dict[str, str]) -> str:
    seen = set()
    while value.startswith("#") and value[1:] in textures and value not in seen:
        seen.add(value)
        value = textures[value[1:]]
    if value.startswith("#"):
        return MISSING_TEXTURE
    if ":" in value:
        namespace, path = value.split(":", 1)
        return path if namespace == "minecraft" else MISSING_TEXTURE
    return value


def _material_from_name(block_name: str) -> dict[str, str]:
    short = block_name.removeprefix("minecraft:")
    if short in _AUTO_CUBE_BLOCKS:
        texture = _auto_texture_name(block_name)
        return _all_faces(texture)
    _MISSING_BLOCKS.add(block_name)
    return _all_faces(MISSING_TEXTURE)


def _auto_texture_name(block_name: str) -> str:
    return "block/" + block_name.removeprefix("minecraft:")


def _all_faces(texture: str) -> dict[str, str]:
    return {face: texture for face in ("north", "south", "east", "west", "up", "down")} | {"all": texture}


def _column(side: str, top: str) -> dict[str, str]:
    return {
        "north": side,
        "south": side,
        "east": side,
        "west": side,
        "up": top,
        "down": top,
        "all": side,
    }


def _cube_bottom_top(side: str, top: str, bottom: str) -> dict[str, str]:
    return {
        "north": side,
        "south": side,
        "east": side,
        "west": side,
        "up": top,
        "down": bottom,
        "all": side,
    }


def _leaves(name: str) -> dict[str, str]:
    _APPROXIMATE_BLOCKS.add("minecraft:" + name)
    return _all_faces("block/" + name)


def _water() -> dict[str, str]:
    _APPROXIMATE_BLOCKS.add("minecraft:water")
    return _all_faces("block/water_still")


_AUTO_CUBE_BLOCKS = {
    "stone",
    "granite",
    "polished_granite",
    "diorite",
    "polished_diorite",
    "andesite",
    "polished_andesite",
    "deepslate",
    "cobbled_deepslate",
    "tuff",
    "calcite",
    "dripstone_block",
    "dirt",
    "coarse_dirt",
    "podzol",
    "rooted_dirt",
    "mud",
    "clay",
    "sand",
    "red_sand",
    "gravel",
    "cobblestone",
    "mossy_cobblestone",
    "bedrock",
    "coal_ore",
    "iron_ore",
    "copper_ore",
    "gold_ore",
    "redstone_ore",
    "emerald_ore",
    "lapis_ore",
    "diamond_ore",
    "deepslate_coal_ore",
    "deepslate_iron_ore",
    "deepslate_copper_ore",
    "deepslate_gold_ore",
    "deepslate_redstone_ore",
    "deepslate_emerald_ore",
    "deepslate_lapis_ore",
    "deepslate_diamond_ore",
    "netherrack",
    "soul_sand",
    "soul_soil",
    "obsidian",
    "moss_block",
    "snow_block",
    "ice",
    "packed_ice",
    "blue_ice",
    "white_wool",
    "black_wool",
    "gray_wool",
    "light_gray_wool",
    "brown_wool",
    "red_wool",
    "orange_wool",
    "yellow_wool",
    "lime_wool",
    "green_wool",
    "cyan_wool",
    "light_blue_wool",
    "blue_wool",
    "purple_wool",
    "magenta_wool",
    "pink_wool",
}

_APPROXIMATE_BLOCKS: set[str] = set()
_MISSING_BLOCKS: set[str] = set()

_MATERIALS: dict[str, dict[str, str]] = {
    "minecraft:air": _all_faces(MISSING_TEXTURE),
    "minecraft:cave_air": _all_faces(MISSING_TEXTURE),
    "minecraft:void_air": _all_faces(MISSING_TEXTURE),
    "minecraft:grass_block": _cube_bottom_top("block/grass_block_side", "block/grass_block_top", "block/dirt"),
    "minecraft:snowy_grass_block": _cube_bottom_top("block/grass_block_snow", "block/snow", "block/dirt"),
    "minecraft:dirt": _all_faces("block/dirt"),
    "minecraft:stone": _all_faces("block/stone"),
    "minecraft:water": _water(),
    "minecraft:oak_leaves": _leaves("oak_leaves"),
    "minecraft:birch_leaves": _leaves("birch_leaves"),
    "minecraft:spruce_leaves": _leaves("spruce_leaves"),
    "minecraft:jungle_leaves": _leaves("jungle_leaves"),
    "minecraft:acacia_leaves": _leaves("acacia_leaves"),
    "minecraft:dark_oak_leaves": _leaves("dark_oak_leaves"),
    "minecraft:mangrove_leaves": _leaves("mangrove_leaves"),
    "minecraft:cherry_leaves": _leaves("cherry_leaves"),
    "minecraft:azalea_leaves": _leaves("azalea_leaves"),
    "minecraft:flowering_azalea_leaves": _leaves("flowering_azalea_leaves"),
}

for _name in _AUTO_CUBE_BLOCKS:
    _MATERIALS.setdefault("minecraft:" + _name, _all_faces("block/" + _name))

for _wood in ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry"):
    _MATERIALS["minecraft:" + _wood + "_log"] = _column("block/" + _wood + "_log", "block/" + _wood + "_log_top")
    _MATERIALS["minecraft:stripped_" + _wood + "_log"] = _column("block/stripped_" + _wood + "_log", "block/stripped_" + _wood + "_log_top")
    _MATERIALS["minecraft:" + _wood + "_wood"] = _all_faces("block/" + _wood + "_log")
    _MATERIALS["minecraft:stripped_" + _wood + "_wood"] = _all_faces("block/stripped_" + _wood + "_log")
    _MATERIALS["minecraft:" + _wood + "_planks"] = _all_faces("block/" + _wood + "_planks")

for _ore in list(_AUTO_CUBE_BLOCKS):
    _MATERIALS.setdefault("minecraft:" + _ore, _all_faces("block/" + _ore))
