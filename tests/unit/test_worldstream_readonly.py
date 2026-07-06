from __future__ import annotations

import json
from pathlib import Path


PRODUCTION_ROOT = Path("body-mod/src/main/java")
WORLDSTREAM_ROOT = Path("body-mod/src/main/java/dev/minebot/bridge/worldstream")
BRIDGE_VERSION_ROOT = Path("body-mod/src/main/java/dev/minebot/bridge/version")
FABRIC_MOD_JSON = Path("body-mod/src/main/resources/fabric.mod.json")

FORBIDDEN_WORLDSTREAM_TOKENS = (
    "setBlock(",
    "removeBlock(",
    "destroyBlock(",
    "addFreshEntity(",
    "discard(",
    "teleportTo(",
    "setDeltaMovement(",
    "FakePlayer.get(",
    "damageSources(",
    "setHealth(",
    "getInventory().set",
    "performCommand(",
    "createCommandSourceStack(",
)


def test_worldstream_package_has_no_mutating_server_calls() -> None:
    offenders: list[str] = []
    for path in sorted(WORLDSTREAM_ROOT.rglob("*.java")):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_WORLDSTREAM_TOKENS:
            if token in text:
                offenders.append(f"{path}:{token}")
    assert offenders == []


def test_production_source_set_has_no_mutating_server_calls() -> None:
    offenders: list[str] = []
    for path in sorted(PRODUCTION_ROOT.rglob("*.java")):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_WORLDSTREAM_TOKENS:
            if token in text:
                offenders.append(f"{path}:{token}")
    assert offenders == []


def test_world_access_stage0_reads_only() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(BRIDGE_VERSION_ROOT.rglob("*.java")))
    for token in FORBIDDEN_WORLDSTREAM_TOKENS:
        assert token not in text
    assert "getBlockState(" in text
    assert "getPlayerList()" in text


def test_bridge_entrypoint_replaces_old_body_poc() -> None:
    manifest = json.loads(FABRIC_MOD_JSON.read_text(encoding="utf-8"))
    entrypoints = manifest["entrypoints"]["server"]
    assert entrypoints == ["dev.minebot.bridge.MineBotBridgeMod"]
