from __future__ import annotations

import ast
import json
import re
from pathlib import Path


CAMERA_ROOT = Path("minebot/camera")
BRIDGE_ROOT = Path("minecraft/server/bridge/src/main/java/dev/minebot/bridge")
COMMON_ROOT = Path("minecraft/server/common/src/main/java/dev/minebot/server/common/transport")
JAVA_ROOT = Path("minecraft/server/bridge/src/main/java")
FABRIC_MOD = Path("minecraft/server/bridge/src/main/resources/fabric.mod.json")
CLIENT_ROOT = Path("minecraft/camera/client/src/main/java/dev/minebot/camera/client")
CLIENT_MOD = Path("minecraft/camera/client/src/main/resources/fabric.mod.json")


def test_camera_package_is_independent_of_agent_brain_body_and_game() -> None:
    forbidden = ("minebot.app", "minebot.brain", "minebot.body", "minebot.game")
    offenders: list[str] = []
    for path in CAMERA_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{path}:{alias.name}"
                    for alias in node.names
                    if alias.name.startswith(forbidden)
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden):
                    offenders.append(f"{path}:{module}")
    assert offenders == []






















def test_observer_interactions_are_denied_server_side() -> None:
    source = Path(
        "minecraft/server/bridge/src/main/java/dev/minebot/bridge/observercontrol/ObserverInteractionPolicy.java"
    ).read_text(encoding="utf-8")

    assert "ServerPlayConnectionEvents.JOIN.register" in source
    assert "enforceObserverPolicy" in source
    for callback in (
        "AttackBlockCallback.EVENT.register",
        "AttackEntityCallback.EVENT.register",
        "UseBlockCallback.EVENT.register",
        "UseEntityCallback.EVENT.register",
        "UseItemCallback.EVENT.register",
        "PlayerBlockBreakEvents.BEFORE.register",
        "ServerMessageEvents.ALLOW_CHAT_MESSAGE.register",
        "ServerMessageEvents.ALLOW_COMMAND_MESSAGE.register",
    ):
        assert callback in source
    assert "InteractionResult.FAIL" in source
    assert "denialCountsJson" in source
    assert "CommandManager" not in source

    packet_policy = Path(
        "minecraft/server/bridge/src/main/java/dev/minebot/bridge/mixin/ObserverPacketPolicyMixin.java"
    ).read_text(encoding="utf-8")
    for handler in (
        "handleMovePlayer",
        "handlePlayerAction",
        "handleUseItemOn",
        "handleUseItem",
        "handleInteract",
        "handleContainerClick",
        "handleSetCreativeModeSlot",
        "handleChat",
        "handleChatCommand",
        "handleSignedChatCommand",
    ):
        assert f'method = "{handler}"' in packet_policy
    assert "handleAcceptTeleportation" not in packet_policy


def test_explicit_detach_keeps_online_observer_semantically_hidden() -> None:
    source = Path(
        "minecraft/server/bridge/src/main/java/dev/minebot/bridge/version/Mojmap2612ObserverAccess.java"
    ).read_text(encoding="utf-8")
    detach = source[source.index("public void detach(") : source.index("private static ServerPlayer", source.index("public void detach("))]

    assert "if (disconnect)" in detach
    assert "observer.removeTag(CAMERA_TAG);" in detach
    assert "else {" in detach
    assert "enforceObserverPolicy(server, observer);" in detach
