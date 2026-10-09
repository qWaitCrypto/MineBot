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
from minebot.game.real_client_ipc import RealClientRejected, RealClientTerminal


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
            "inventory_slots": [
                {"slot": slot, "item": "minecraft:oak_log" if slot in {2, 27} else "minecraft:wheat_seeds" if slot == 9 else None,
                 "count": 1 if slot in {2, 27} else 3 if slot == 9 else 0,
                 "empty": slot not in {2, 9, 27}}
                for slot in range(46)
            ],
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
        if scope == "findBlocks":
            return {"ok": True, "complete": True, "data": {"matches": [{"x":4,"y":70,"z":9,"type":"minecraft:oak_log"}]}}
        return {
            "type": "world_read",
            "request_id": "world-1",
            "ok": True,
            "complete": True,
            "data": {"x": 4, "y": 70, "z": 9, "type": "minecraft:oak_log", "state": "SOLID"}
            if scope == "blockAt" else {"cells": []},
        }

    def command(self, objective: str, *, action_id: str, timeout_ms: int, cancel_after_s=None, on_progress=None) -> RealClientTerminal:
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
    assert len(inventory.data["slots"]) == 46
    assert inventory.data["slots"][2]["item"] == "minecraft:oak_log"
    assert inventory.data["slots"][27]["item"] == "minecraft:oak_log"
    assert inventory.data["slots"][0]["empty"]


def test_real_client_inventory_pages_preserve_sparse_slot_ids() -> None:
    body = RealClientBody("CapProbe2", FakeClientIpc())  # type: ignore[arg-type]
    page = body.perceive("inventory", {"start": 24, "limit": 12})
    assert not page.complete and page.next == "36"
    assert page.data["totalSlots"] == 46
    assert [slot["slot"] for slot in page.data["slots"]] == list(range(24, 36))
    assert page.data["slots"][3]["count"] == 1


def test_real_client_does_not_invent_slots_from_aggregate_counts() -> None:
    class OldIpc(FakeClientIpc):
        def state(self):
            state = super().state()
            state.pop("inventory_slots")
            return state
    body = RealClientBody("CapProbe2", OldIpc())  # type: ignore[arg-type]
    assert body.perceive("inventory", {}).error == "capability_unavailable:client_inventory_slots_missing"


def test_real_client_container_pages_share_one_native_menu_snapshot() -> None:
    class MenuIpc(FakeClientIpc):
        def control(self, request_type, **fields):
            self.controls.append((request_type, fields))
            return {"ok": True, "data": {"menu_id": 7, "slots": [{"slot": i, "empty": True, "count": 0} for i in range(27)]}}
    ipc = MenuIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    for start in (0, 12, 24):
        page = body.perceive("container", {"pos": [1, 70, 2], "start": start, "limit": 12})
        assert page.ok and page.data["menu_id"] == 7
    assert len(ipc.controls) == 1
    assert [slot["slot"] for slot in page.data["slots"]] == [24, 25, 26]
    assert page.complete
    body.perceive("container", {"pos": [1, 70, 2], "start": 0})
    assert len(ipc.controls) == 2


def test_real_client_recipe_read_preserves_ingredient_alternatives() -> None:
    class RecipeIpc(FakeClientIpc):
        def world_read(self, scope, params):
            assert scope == "recipeData" and params == {"item": "stick"}
            return {"ok": True, "data": {"variants": [{"output_item": "minecraft:stick", "output_count": 4,
                    "ingredient_groups": [["minecraft:oak_planks", "minecraft:birch_planks"]] * 2}]}}
    body = RealClientBody("CapProbe2", RecipeIpc())  # type: ignore[arg-type]
    page = body.perceive("recipeData", {"item": "stick"})
    assert page.ok and page.data["variants"][0]["ingredient_groups"][0] == ["minecraft:oak_planks", "minecraft:birch_planks"]


def test_real_client_world_reads_keep_existing_shapes() -> None:
    body = RealClientBody("CapProbe2", FakeClientIpc())  # type: ignore[arg-type]

    blocks = body.perceive("findBlocks", {"block_types": ["minecraft:oak_log"]})
    assert blocks.ok
    assert blocks.data["matches"] == [{"x": 4, "y": 70, "z": 9, "type": "minecraft:oak_log"}]
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
        ({"kind": "near", "pos": [10, 70, 9], "radius": 0}, "near:10:70:9:0"),
        ({"kind": "near", "pos": [10,70,9], "radius":0.5}, "near:10:70:9:0"),
        ({"kind":"composite","goals":[{"kind":"near","pos":[10,70,9],"radius":0.5}]}, "any:near:10:70:9:0"),
        ({"kind": "block", "x": 10, "y": 70, "z": 9}, "block:10:70:9"),
        ({"kind": "composite", "mode": "any", "goals": [{"kind": "near", "pos": [10, 70, 9], "radius": 0}]},
         "any:near:10:70:9:0"),
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
    ("engageEntity", "rangedAttack", "craftItem", "containerTransfer", "furnaceTransfer", "handoffItem", "ascend", "digDown", "digUp", "useItem", "placeBlock"),
)
def test_real_client_body_routes_structured_actions_to_typed_client_wire(
    action_name: str,
) -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    params = {"timeout_ticks": 20}
    if action_name == "useItem":
        params.update(mode="continuous", ticks=80)
    accepted = body.execute(Action(f"structured-{action_name}", action_name, params))
    terminal = body.await_action_terminal(f"structured-{action_name}", timeout_s=1.0)

    assert accepted.ok and accepted.accepted
    assert terminal.data["success"] is True
    assert ipc.actions[0][0] == action_name
    if action_name == "useItem":
        assert ipc.actions[0][1]["mode"] == "continuous" and ipc.actions[0][1]["ticks"] == 80


@pytest.mark.parametrize("name", ("mineBlock", "collectBlock"))
def test_real_client_body_adapts_exact_collect_block_without_fallback(name) -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]

    result = body.execute(Action("a3", name, {"target": [1, 2, 3]}))
    terminal = body.await_action_terminal("a3", timeout_s=1.0)

    assert result.ok and result.accepted
    assert terminal.name == "mineDone"
    assert ipc.commands[0][0] == "clear:1:2:3:1:2:3"


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


@pytest.mark.parametrize("action_name", ("builderBuildFile", "baritoneBuilderBuildFile"))
def test_schematic_build_owns_body_until_native_terminal_and_can_be_canceled(action_name) -> None:
    class BuildingIpc(FakeClientIpc):
        def action(self, name, params, *, action_id, timeout_ms, on_progress=None):
            self.actions.append((name, params, action_id, timeout_ms))
            assert self._cancel.wait(2)
            return RealClientTerminal(action_id, "canceled", "cancel_requested", self.state())

    ipc = BuildingIpc()
    body = RealClientBody("CapProbe2", ipc)
    action = Action("schematic-1", action_name, {"path": "fixture.schem", "origin": [1, 70, 2]})
    assert body.execute(action).accepted
    assert not body.execute(Action("move-2", "navigate", {"goal": {"kind": "xz", "x": 10, "z": 20}})).accepted
    body.interrupt("user_stop")
    terminal = body.await_action_terminal(action.id, timeout_s=1)
    assert terminal.data["status"] == "canceled"
    assert ipc.actions[0][0] == "builderBuildFile"
    assert ipc.canceled == [action.id]
    assert not ipc.controls
    assert body.execute(Action("move-3", "navigate", {"goal": {"kind": "xz", "x": 10, "z": 20}})).accepted


@pytest.mark.parametrize(
    "action_name",
    ("jump", "stop", "selectSlot", "lookAt", "attackEntity", "dropItem", "moveItem", "igniteBlock", "sowCrop"),
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


@pytest.mark.parametrize("error, reason", [
    (RealClientRejected("destination_occupied"), "destination_occupied"),
    (ConnectionError("closed"), "transport:ConnectionError"),
])
def test_real_client_primitive_refusal_is_distinct_from_transport_failure(error, reason) -> None:
    class RefusingIpc(FakeClientIpc):
        def control(self, request_type, **fields):
            raise error
    body = RealClientBody("CapProbe2", RefusingIpc())  # type: ignore[arg-type]
    action = Action.create("moveItem", {"from_slot": 9, "to_slot": 0, "count": 1})
    assert body.execute(action).accepted
    terminal = body.await_action_terminal(action.id, timeout_s=1.0)
    assert not terminal.data["success"]
    assert terminal.data["status"] == "failed"
    assert terminal.data["reason"] == reason


@pytest.mark.parametrize(
    ("action_name", "params", "objective_prefix"),
    (
        ("mineResource", {"block": "oak_log"}, "mine:oak_log"),
        ("getToBlock", {"block": "crafting_table"}, "get:crafting_table"),
        ("explore", {"anchor": [8, 70, 7]}, "explore:8:7"),
        ("followEntity", {"target": "Sheep", "keep_radius": 1.25}, "follow:Sheep:0:1.25"),
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
def test_real_client_body_maps_process_objectives(
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
    assert ipc.commands[0][0] == "near:10:70:9:0"


def test_real_client_follow_uses_baritone_target_resolution() -> None:
    body = RealClientBody("CapProbe2", FakeClientIpc())  # type: ignore[arg-type]
    result = ObjectiveNavigationTransactions(body).follow_entity(
        "Sheep",
        keep_distance=4,
        timeout_s=1,
    )

    assert result.success
    assert result.metrics["requested_target"] == "Sheep"


@pytest.mark.parametrize("tool_name, action_name", [("engage_entity", "engageEntity"), ("ranged_attack", "rangedAttack")])
def test_real_client_combat_tools_send_one_resolved_uuid(tool_name, action_name) -> None:
    target_uuid = "11111111-1111-4111-8111-111111111111"

    class CombatIpc(FakeClientIpc):
        def state(self):
            state = super().state()
            state["entities"].append({"id": "22222222-2222-4222-8222-222222222222", "name": "Decoy", "type": "zombie",
                                      "hostile": True, "dist2": 0.01, "pos": [4, 70, 9]})
            state["entities"].append({"id": "00000000-0000-4000-8000-000000000000", "name": "Sheep", "type": "sheep",
                                      "health": 0, "dist2": 0.01, "pos": [4, 70, 9]})
            return state

        def action(self, name, params, *, action_id, timeout_ms, **kwargs):
            super().action(name, params, action_id=action_id, timeout_ms=timeout_ms, **kwargs)
            return RealClientTerminal(action_id, "completed", "killed" if name == "engageEntity" else "damage_observed",
                                      {"target_id": params["target_uuid"], "target_health": 0 if name == "engageEntity" else 15,
                                       "damage_observed": True, "shots": 1})

    ipc = CombatIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client"))
    result = registry.get(tool_name).callable({"target": "Sheep", "timeout_s": 1})
    assert result.success
    assert result.reason == ("killed" if tool_name == "engage_entity" else "damage_observed")
    assert ipc.actions[0][0] == action_name
    assert ipc.actions[0][1]["target_uuid"] == target_uuid
    assert result.metrics["target_id"] == target_uuid
    assert not ipc.commands


def test_real_client_ranged_tool_preserves_no_ammunition_and_target_mismatch() -> None:
    class FailedShotIpc(FakeClientIpc):
        failure = True

        def action(self, name, params, *, action_id, timeout_ms, **kwargs):
            return RealClientTerminal(action_id, "failed" if self.failure else "completed",
                                      "no_ammunition" if self.failure else "damage_observed",
                                      {"target_id": params["target_uuid"] if self.failure else "22222222-2222-4222-8222-222222222222"})
    ipc = FailedShotIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client"))
    result = registry.get("ranged_attack").callable({"target": "Sheep"})
    assert not result.success and result.reason == "no_ammunition"
    ipc.failure = False
    result = registry.get("ranged_attack").callable({"target": "Sheep"})
    assert not result.success and result.reason == "target_identity_mismatch"


def test_real_client_ranged_tool_missing_target_does_not_start_an_action() -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client"))
    result = registry.get("ranged_attack").callable({"target": "Missing"})
    assert not result.success and result.reason == "target_not_found"
    assert not ipc.actions


@pytest.mark.parametrize("tool_name, params, action_name", [
    ("go_to_surface", {"timeout_s": 2}, "ascend"),
    ("dig_down", {"target_y": 65, "max_steps": 3}, "digDown"),
    ("dig_up", {"target_y": 75, "max_steps": 3, "scaffold_blocks": ["dirt"]}, "digUp"),
])
def test_real_client_vertical_tools_wait_for_one_body_terminal(tool_name, params, action_name) -> None:
    class VerticalIpc(FakeClientIpc):
        def action(self, name, params, *, action_id, timeout_ms, **kwargs):
            super().action(name, params, action_id=action_id, timeout_ms=timeout_ms, **kwargs)
            time.sleep(0.01)
            return RealClientTerminal(action_id, "completed", "surface_reached" if name == "ascend" else "target_reached",
                                      {"position": [4.5, params.get("target_y", 80), 9.5], "on_ground": True})

    ipc = VerticalIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client"))
    result = registry.get(tool_name).callable(params)
    assert result.success and result.metrics["on_ground"]
    assert len(ipc.actions) == 1 and ipc.actions[0][0] == action_name
    if action_name != "ascend":
        assert ipc.actions[0][1]["max_distance"] == 3
        assert ipc.actions[0][1]["max_mutations"] == 9
    if action_name == "digUp":
        assert ipc.actions[0][1]["scaffold_blocks"] == ["dirt"]
    assert not ipc.commands


def test_real_client_vertical_tool_returns_cancellation_terminal() -> None:
    started = threading.Event()

    class CancellableVerticalIpc(FakeClientIpc):
        def action(self, name, params, *, action_id, timeout_ms, **kwargs):
            started.set()
            assert self._cancel.wait(2)
            return RealClientTerminal(action_id, "canceled", "cancel_requested", {"position": [4.5, 70, 9.5]})

    ipc = CancellableVerticalIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client"))
    results = []
    worker = threading.Thread(target=lambda: results.append(registry.get("dig_up").callable({"target_y": 75})))
    worker.start()
    assert started.wait(2)
    assert body.interrupt("stop vertical work").ok
    worker.join(2)
    assert not worker.is_alive()
    assert len(results) == 1 and not results[0].success and results[0].reason == "cancel_requested"
    assert len(ipc.canceled) == 1


def test_real_client_vertical_tool_retains_liquid_refusal() -> None:
    class HazardIpc(FakeClientIpc):
        def action(self, name, params, *, action_id, timeout_ms, **kwargs):
            return RealClientTerminal(action_id, "failed", "vertical_liquid_hazard", {"mutations": 0})

    body = RealClientBody("CapProbe2", HazardIpc())  # type: ignore[arg-type]
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client"))
    result = registry.get("dig_down").callable({"target_y": 65})
    assert not result.success and result.reason == "vertical_liquid_hazard" and result.metrics["mutations"] == 0


def test_state_preserves_clock_sleep_weather_and_effects():
    ipc = FakeClientIpc()
    snapshot = ipc.state() | {"time": 18000, "sleeping": True, "weather": "rain",
                              "effects": [{"id": "minecraft:speed", "amplifier": 0, "duration": 200}]}
    ipc.state = lambda: snapshot
    body = RealClientBody("Bot", ipc)
    state = body.get_state()
    assert (state.time, state.sleeping, state.weather, state.effects) == (
        18000, True, "rain", snapshot["effects"])
    body.close()


def test_interrupt_stops_native_process_without_pending_python_action():
    ipc = FakeClientIpc()
    snapshot = ipc.state() | {"any_process_active": True}
    ipc.state = lambda: snapshot
    body = RealClientBody("Bot", ipc)
    result = body.interrupt("stop file build")
    assert result.ok and result.data["native_process_stop"]
    assert ipc.controls[-1] == ("primitive", {"action_name": "stop", "params": {}})
    body.close()
