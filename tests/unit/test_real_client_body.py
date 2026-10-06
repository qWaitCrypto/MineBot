from __future__ import annotations

import threading
import time

import pytest

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.body.objective_navigation import ObjectiveNavigationTransactions
from minebot.contract import Action
from minebot.contract import Region
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientTerminal


class FakeClientIpc:
    def __init__(self) -> None:
        self.connected = False
        self.commands: list[tuple[str, str, int]] = []
        self.actions: list[tuple[str, dict[str, object], str, int]] = []
        self.controls: list[tuple[str, dict[str, object]]] = []
        self.canceled: list[str] = []
        self._cancel = threading.Event()

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def state(self) -> dict[str, object]:
        return {
            "position": [4.5, 70.0, 9.5],
            "health": 20.0,
            "food": 20,
            "oxygen": 300,
            "yaw": 90.0,
            "pitch": -5.0,
            "selected_slot": 2,
            "selected_item": "minecraft:oak_log",
            "offhand_item": "minecraft:torch",
            "dimension": "minecraft:overworld",
            "inventory": {"oak_log": 2, "wheat_seeds": 3},
            "entities": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "name": "Sheep",
                    "type": "sheep",
                    "pos": [5.0, 70.0, 9.0],
                    "dist2": 0.25,
                    "hostile": False,
                }
            ],
        }

    def scan(self, target: str) -> dict[str, object]:
        return {"locations": [[4, 70, 9]], "target": target}

    def world_read(self, scope: str, params: dict[str, object]) -> dict[str, object]:
        del params
        return {
            "type": "world_read",
            "request_id": "world-1",
            "ok": True,
            "complete": True,
            "data": {"x": 4, "y": 70, "z": 9, "type": "minecraft:oak_log", "state": "SOLID"}
            if scope == "blockAt" else {"cells": []},
        }

    def command(self, objective: str, *, action_id: str, timeout_ms: int, cancel_after_s=None) -> RealClientTerminal:
        del cancel_after_s
        self.commands.append((objective, action_id, timeout_ms))
        if self._cancel.wait(0.02):
            return RealClientTerminal(
                action_id=action_id,
                status="canceled",
                reason="cancel_requested",
                payload={"position": [4.5, 70.0, 9.5]},
            )
        return RealClientTerminal(
            action_id=action_id,
            status="completed",
            reason="goal_reached",
            payload={"position": [10.5, 70.0, 9.5]},
        )

    def cancel(self, action_id: str) -> None:
        self.canceled.append(action_id)
        self._cancel.set()

    def action(
        self,
        name: str,
        params: dict[str, object],
        *,
        action_id: str,
        timeout_ms: int,
        on_progress=None,
    ) -> RealClientTerminal:
        del on_progress
        self.actions.append((name, dict(params), action_id, timeout_ms))
        return RealClientTerminal(
            action_id=action_id,
            status="completed",
            reason="verified",
            payload={"position": [4.5, 70.0, 9.5], "action_kind": name},
        )

    def control(self, request_type: str, **fields: object) -> dict[str, object]:
        self.controls.append((request_type, dict(fields)))
        return {
            "type": request_type,
            "request_id": "control-1",
            "position": [4.5, 70.0, 9.5],
            "inventory": {"oak_log": 2, "wheat_seeds": 3},
            "allow_break": fields.get("allow_break", True),
        }


REGION = Region("test", (-64, -64, -64), (64, 320, 64))


def test_real_client_body_maps_state_inventory_and_find_blocks() -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]

    state = body.get_state()
    assert state.pos == (4.5, 70.0, 9.5)
    assert state.yaw == 90.0 and state.pitch == -5.0
    assert state.oxygen == 300 and state.selected_slot == 2
    assert state.selected_item == "oak_log" and state.offhand_item == "torch"
    assert state.inventory_counts == {"oak_log": 2, "wheat_seeds": 3}

    inventory = body.perceive("inventory", {"limit": 46})
    assert inventory.ok
    assert {slot["item"] for slot in inventory.data["slots"]} == {"oak_log", "wheat_seeds"}

    blocks = body.perceive("findBlocks", {"block_types": ["minecraft:oak_log"]})
    assert blocks.ok
    assert blocks.data["matches"] == [{"x": 4, "y": 70, "z": 9, "type": "oak_log"}]
    entities = body.perceive("nearbyEntities", {"limit": 4})
    assert entities.ok
    assert entities.data["entities"][0]["name"] == "Sheep"
    block = body.perceive("blockAt", {"x": 4, "y": 70, "z": 9})
    assert block.ok
    assert block.data["type"] == "minecraft:oak_log"

    capabilities = body.perceive("capabilities", {})
    assert capabilities.ok
    assert not any(item["action"] == "craftItem" for item in capabilities.data["unsupported"])
    assert any(item["action"] == "baritoneCommand" for item in capabilities.data["unsupported"])


def test_real_client_body_executes_process_and_returns_terminal_event() -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    action = Action("a1", "navigate", {"goal": {"kind": "near", "x": 10, "y": 70, "z": 9, "range": 1}})

    result = body.execute(action)
    assert result.ok and result.accepted and not result.complete
    terminal = body.await_action_terminal(action.id, timeout_s=1.0)

    assert ipc.commands == [("near:10:70:9:1", "a1", 60000)]
    assert terminal.name == "navigateDone"
    assert terminal.data["success"] is True
    assert body.poll_events() == [terminal]


@pytest.mark.parametrize(
    ("goal", "objective"),
    (
        ({"kind": "block", "x": 10, "y": 70, "z": 9}, "block:10:70:9"),
        (
            {
                "kind": "composite",
                "mode": "any",
                "goals": [{"kind": "block", "x": 10, "y": 70, "z": 9}],
            },
            "any:block:10:70:9",
        ),
    ),
)
def test_real_client_body_preserves_block_goal_semantics(
    goal: dict[str, object], objective: str,
) -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]

    accepted = body.execute(Action("goal-shape", "navigate", {"goal": goal}))
    body.await_action_terminal("goal-shape", timeout_s=1.0)

    assert accepted.ok and accepted.accepted
    assert ipc.commands[0][0] == objective


def test_real_client_body_interrupts_active_process() -> None:
    ipc = FakeClientIpc()
    ipc._cancel.clear()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    action = Action("a2", "baritoneMine", {"block": "oak_log", "timeout_s": 5})

    body.execute(action)
    time.sleep(0.005)
    result = body.interrupt("user_cancel")
    terminal = body.await_action_terminal(action.id, timeout_s=1.0)

    assert result.ok and result.accepted
    assert ipc.canceled == ["a2"]
    assert terminal.data["status"] == "canceled"


@pytest.mark.parametrize(
    "action_name",
    ("engageEntity", "rangedAttack", "craftItem", "containerTransfer", "furnaceTransfer", "handoffItem", "ascend", "digDown", "digUp"),
)
def test_real_client_body_routes_structured_actions_to_typed_client_wire(
    action_name: str,
) -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    accepted = body.execute(Action(f"structured-{action_name}", action_name, {"timeout_ticks": 20}))
    terminal = body.await_action_terminal(f"structured-{action_name}", timeout_s=1.0)

    assert accepted.ok and accepted.accepted
    assert terminal.data["success"] is True
    assert ipc.actions[0][0] == action_name


def test_real_client_body_adapts_exact_collect_block_without_fallback() -> None:
    body = RealClientBody("CapProbe2", FakeClientIpc())  # type: ignore[arg-type]

    result = body.execute(Action("a3", "collectBlock", {"target": [1, 2, 3]}))
    terminal = body.await_action_terminal("a3", timeout_s=1.0)

    assert result.ok and result.accepted
    assert terminal.name == "mineDone"


def test_real_client_body_maps_baritone_controls_through_the_same_body_boundary() -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]

    action = Action("settings-1", "baritoneSettings", {"settings": {"allow_break": False}})
    accepted = body.execute(action)
    terminal = body.await_action_terminal(action.id, timeout_s=1.0)

    assert accepted.ok and accepted.accepted
    assert terminal.name == "baritoneSettingsDone"
    assert terminal.data["success"] is True
    assert ipc.controls == [("settings", {"allow_break": False})]


@pytest.mark.parametrize(
    ("action_name", "params", "request_type"),
    (
        ("getToBlockBlacklistClosest", {}, "get_to_block_blacklist"),
        ("builderBuildFile", {"path": "fixture.schem", "origin": [1, 70, 2]}, "builder_file"),
        ("builderSelectionAdd", {"pos1": [1, 70, 2], "pos2": [3, 72, 4]}, "builder_selection"),
        ("builderSelectionClear", {}, "builder_selection"),
        (
            "builderSelectionExpand",
            {"direction": "up", "amount": 2},
            "builder_selection",
        ),
    ),
)
def test_real_client_body_maps_extended_baritone_controls(
    action_name: str,
    params: dict[str, object],
    request_type: str,
) -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]

    accepted = body.execute(Action(f"control-{action_name}", action_name, params))
    terminal = body.await_action_terminal(f"control-{action_name}", timeout_s=1.0)

    assert accepted.ok and accepted.accepted
    assert terminal.data["success"] is True
    assert ipc.controls[0][0] == request_type


@pytest.mark.parametrize(
    "action_name",
    ("jump", "stop", "selectSlot", "lookAt", "useItem", "attackEntity", "dropItem", "moveItem", "mineBlock", "placeBlock", "igniteBlock", "sowCrop"),
)
def test_real_client_body_routes_native_body_actions_to_client_primitive(
    action_name: str,
) -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    params: dict[str, object] = {}
    if action_name == "lookAt":
        params["target"] = [5.0, 70.0, 9.0]
    elif action_name == "selectSlot":
        params["slot"] = 0
    elif action_name == "mineBlock":
        params["target"] = [4, 70, 9]
    elif action_name == "placeBlock":
        params["target"] = [4, 70, 9]
    elif action_name in {"igniteBlock", "sowCrop"}:
        params["target"] = [4, 70, 9]
    action = Action(f"primitive-{action_name}", action_name, params)
    accepted = body.execute(action)
    terminal = body.await_action_terminal(action.id, timeout_s=1.0)
    assert accepted.ok and accepted.accepted
    assert terminal.data["success"] is True
    assert ipc.controls[0][0] == "primitive"


@pytest.mark.parametrize(
    ("action_name", "params", "objective_prefix"),
    (
        ("mineResource", {"block": "oak_log"}, "mine:oak_log"),
        ("getToBlock", {"block": "crafting_table"}, "get:crafting_table"),
        ("explore", {"anchor": [8, 70, 7]}, "explore:8:7"),
        ("followEntity", {"target": "Sheep"}, "follow:Sheep"),
        ("pickupItems", {"item": "oak_log"}, "pickup:oak_log"),
        ("farmArea", {"anchor": [8, 70, 7], "range": 12}, "farm:12:8:70:7"),
        (
            "buildStructure",
            {"origin": [8, 70, 7], "size": [2, 3, 2], "block": "stone"},
            "build:stone:8:70:7:2:3:2",
        ),
        ("clearArea", {"start": [8, 70, 7], "end": [9, 71, 8]}, "clear:8:70:7:9:71:8"),
        ("elytraTravel", {"target": [80, 90, 7]}, "elytra:80:90:7"),
    ),
)
def test_real_client_body_exposes_verified_process_families(
    action_name: str,
    params: dict[str, object],
    objective_prefix: str,
) -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]

    action = Action(f"cap-{action_name}", action_name, params)
    accepted = body.execute(action)
    terminal = body.await_action_terminal(action.id, timeout_s=1.0)

    assert accepted.ok and accepted.accepted
    assert terminal.data["success"] is True
    assert ipc.commands[0][0] == objective_prefix


def test_real_client_provider_selects_the_abstract_body_and_routes_move_tool() -> None:
    ipc = FakeClientIpc()
    runtime = build_body_provider(
        "real_client",
        bot_name="CapProbe2",
        natural_region=REGION,
        real_client_ipc=ipc,  # type: ignore[arg-type]
    )

    assert runtime.body is runtime.real_client_body
    assert runtime.java_body is None
    registry = build_phase1_registry(
        runtime.body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="real_client",
            governance_policy=runtime.governance,
        ),
    )
    result = registry.get("move_to").callable({"pos": [10, 70, 9], "timeout_s": 1})

    assert result.success
    assert result.reason == "arrived"
    assert ipc.commands[0][0] == "near:10:70:9:1"


def test_real_client_follow_uses_baritone_target_resolution() -> None:
    body = RealClientBody("CapProbe2", FakeClientIpc())  # type: ignore[arg-type]
    result = ObjectiveNavigationTransactions(body).follow_entity(
        "Sheep",
        keep_distance=4,
        timeout_s=1,
    )

    assert result.success
    assert result.metrics["requested_target"] == "Sheep"
