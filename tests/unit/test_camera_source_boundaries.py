from __future__ import annotations

import ast
import json
import re
from pathlib import Path


CAMERA_ROOT = Path("camera")
BRIDGE_ROOT = Path("body-mod/src/main/java/dev/minebot/bridge")
JAVA_ROOT = Path("body-mod/src/main/java")
FABRIC_MOD = Path("body-mod/src/main/resources/fabric.mod.json")
CLIENT_ROOT = Path("camera-client-mod/src/main/java/dev/minebot/camera/client")
CLIENT_MOD = Path("camera-client-mod/src/main/resources/fabric.mod.json")


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


def test_fabric_entrypoint_has_converged_on_bridge_identity() -> None:
    metadata = json.loads(FABRIC_MOD.read_text(encoding="utf-8"))
    assert metadata["id"] == "minebot-bridge"
    assert metadata["name"] == "MineBot Bridge"
    assert metadata["entrypoints"]["server"] == ["dev.minebot.bridge.MineBotBridgeMod"]


def test_bridge_reuses_lifecycle_router_connection_and_json_envelope() -> None:
    required = {
        "MineBotBridgeMod.java",
        "transport/BridgeChannel.java",
        "transport/BridgeChannelRouter.java",
        "transport/BridgeConnection.java",
        "transport/BridgeWebSocketServer.java",
        "transport/OutboundMessage.java",
    }
    present = {str(path.relative_to(BRIDGE_ROOT)) for path in BRIDGE_ROOT.rglob("*.java")}
    assert required <= present

    connection = (BRIDGE_ROOT / "transport/BridgeConnection.java").read_text(encoding="utf-8")
    assert "seq" in connection
    assert "server_tick" in connection
    assert "sent_at_ms" in connection


def test_observer_control_has_narrow_version_adapter_and_exact_request_surface() -> None:
    required = {
        "observercontrol/ObserverControlChannel.java",
        "observercontrol/ObserverMode.java",
        "version/ObserverAccess.java",
        "version/Mojmap2612ObserverAccess.java",
        "version/ObserverState.java",
        "version/TargetState.java",
    }
    present = {str(path.relative_to(BRIDGE_ROOT)) for path in BRIDGE_ROOT.rglob("*.java")}
    assert required <= present

    source = (BRIDGE_ROOT / "observercontrol/ObserverControlChannel.java").read_text(encoding="utf-8")
    assert '"HELLO"' in source
    assert '"ATTACH"' in source
    assert '"UPDATE"' in source
    assert '"HEARTBEAT"' in source
    assert '"STATUS"' in source
    assert '"DETACH"' in source
    for forbidden_type in ("COMMAND", "ACTION", "MOVE", "ATTACK", "USE", "CHAT"):
        assert f'"{forbidden_type}"' not in source
    assert "MAX_IDEMPOTENCY_ENTRIES" in source
    assert "leaseExpiresAtTick" in source
    assert 'value.addProperty("loaded_chunks"' in source
    assert 'value.addProperty("online_players"' in source
    assert "configuredObserverId" in source
    assert "requireCurrentSession" in source
    assert "requireSuccessorSession" in source
    assert "isCurrentGeneration" in source
    assert "isSuccessorGeneration" in source


def test_observer_control_enforces_request_rate_limit() -> None:
    connection = (BRIDGE_ROOT / "transport/BridgeConnection.java").read_text(encoding="utf-8")
    server = (BRIDGE_ROOT / "transport/BridgeWebSocketServer.java").read_text(encoding="utf-8")
    assert "MAX_REQUESTS_PER_SECOND" in connection
    assert "allowRequest" in connection
    assert "rate_limited" in server


def test_bridge_outbound_work_and_request_size_are_bounded() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in BRIDGE_ROOT.rglob("*.java"))
    assert "MAX_REQUEST_BYTES" in source
    assert "ArrayBlockingQueue" in source or "LinkedBlockingQueue" in source
    assert "newSingleThreadExecutor" not in source


def test_bridge_production_source_has_no_body_or_arbitrary_command_surface() -> None:
    forbidden = {
        "setBlock(": "block mutation",
        "destroyBlock(": "block mutation",
        "getInventory(": "inventory mutation/read coupling",
        ".hurt(": "entity damage",
        ".heal(": "entity healing",
        "performCommand": "command dispatch",
        "getCommands(": "command dispatch",
        "addRegionTicket": "forced chunk ticket",
        "removeRegionTicket": "forced chunk ticket",
        "minebot.body": "Body dependency",
        "minebot.brain": "Brain dependency",
        "minebot.app": "Agent dependency",
        "console-fifo": "server console control",
        "control-script": "arbitrary control script",
    }
    offenders: list[str] = []
    for path in JAVA_ROOT.rglob("*.java"):
        source = path.read_text(encoding="utf-8")
        for token, reason in forbidden.items():
            if token in source:
                offenders.append(f"{path}:{reason}:{token}")
    assert offenders == []


def test_worldstream_is_not_initialized_by_production_bridge() -> None:
    entrypoint = (BRIDGE_ROOT / "MineBotBridgeMod.java").read_text(encoding="utf-8")
    assert "WorldStream" not in entrypoint
    assert "worldstream" not in entrypoint.lower()


def test_observer_access_exposes_only_camera_owned_operations() -> None:
    source = (BRIDGE_ROOT / "version/ObserverAccess.java").read_text(encoding="utf-8")
    allowed = {
        "minecraftVersion",
        "observerState",
        "enforceObserverPolicy",
        "resolveTarget",
        "attachToTarget",
        "holdFixed",
        "sendDirective",
        "detach",
    }
    declared = set(
        re.findall(
            r"^\s*(?:String|ObserverState|TargetState|void)\s+(\w+)\s*\(",
            source,
            flags=re.MULTILINE,
        )
    )
    assert declared == allowed


def test_camera_client_is_client_only_and_receives_directives_without_gameplay_output() -> None:
    metadata = json.loads(CLIENT_MOD.read_text(encoding="utf-8"))
    assert metadata["environment"] == "client"
    assert metadata["entrypoints"] == {
        "client": ["dev.minebot.camera.client.MineBotCameraClient"]
    }
    assert metadata["depends"]["minecraft"] == "26.1.2"
    assert metadata["depends"]["freecam"] == "=1.4.0+mc26.1.2"

    source = "\n".join(path.read_text(encoding="utf-8") for path in CLIENT_ROOT.rglob("*.java"))
    assert "registerGlobalReceiver" in source
    assert "CameraDirectivePayload.TYPE" in source
    assert "PayloadTypeRegistry.serverboundPlay()" in source
    assert source.count("ClientPlayNetworking.send(") == 1
    assert "ClientPlayNetworking.send(new CameraClientTelemetryPayload(telemetry))" in source
    forbidden = {
        "Serverbound": "vanilla gameplay packet output",
        "minecraft.gameMode": "client gameplay interaction",
        "KeyMapping": "key simulation",
        "keyPress": "key simulation",
        "startAttack": "attack input",
        "startUseItem": "use input",
        "sendCommand": "command output",
        "sendChat": "chat output",
    }
    offenders = [f"{reason}:{token}" for token, reason in forbidden.items() if token in source]
    assert offenders == []


def test_camera_client_pins_selected_community_mod_versions() -> None:
    build = Path("camera-client-mod/build.gradle").read_text(encoding="utf-8")
    assert 'maven.modrinth:freecam:dvyNrVvc' in build
    assert 'maven.modrinth:sodium:vf7UgZpC' in build


def test_observer_interactions_are_denied_server_side() -> None:
    source = Path(
        "body-mod/src/main/java/dev/minebot/bridge/observercontrol/ObserverInteractionPolicy.java"
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
        "body-mod/src/main/java/dev/minebot/bridge/mixin/ObserverPacketPolicyMixin.java"
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
        "body-mod/src/main/java/dev/minebot/bridge/version/Mojmap2612ObserverAccess.java"
    ).read_text(encoding="utf-8")
    detach = source[source.index("public void detach(") : source.index("private static ServerPlayer", source.index("public void detach("))]

    assert "if (disconnect)" in detach
    assert "observer.removeTag(CAMERA_TAG);" in detach
    assert "else {" in detach
    assert "enforceObserverPolicy(server, observer);" in detach
