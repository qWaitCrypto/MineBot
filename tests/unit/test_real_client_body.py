from __future__ import annotations

import threading
import time

import pytest

from minebot.app.body_provider import build_body_provider
from minebot.app.body_tool_catalog import WALKING_TOOL_NAMES
from minebot.contract.execution import walk_look_down_scope
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.body.objective_navigation import ObjectiveNavigationTransactions
from minebot.contract import Action
from minebot.contract import Region
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpcError, RealClientNotAccepted, RealClientRejected, RealClientTerminal


class FakeClientIpc:
    def __init__(self) -> None:
        self.connected = False
        self.commands: list[tuple[str, str, int]] = []
        self.actions: list[tuple[str, dict[str, object], str, int]] = []
        self.controls: list[tuple[str, dict[str, object]]] = []
        self.canceled: list[str] = []
        self.look_down: list[bool] = []
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

    def command(self, objective: str, *, action_id: str, timeout_ms: int, cancel_after_s=None, on_progress=None,
                look_down=False) -> RealClientTerminal:
        del cancel_after_s
        self.commands.append((objective, action_id, timeout_ms))
        self.look_down.append(look_down)
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


@pytest.mark.parametrize("sample", [{"ready": False}, {"position": [4, 70, 9]}])
def test_reflex_facts_do_not_turn_unobserved_vitals_into_zero(sample) -> None:
    class MissingSampleIpc(FakeClientIpc):
        def state(self):
            return dict(sample)
    body = RealClientBody("CapProbe2", MissingSampleIpc())
    facts = body.get_reflex_facts()
    assert "health" not in facts
    assert "food" not in facts
    assert "oxygen" not in facts
    assert "time" not in facts and "dimension" not in facts
    if "position" not in sample:
        assert facts["missing"] is True
        assert "pos" not in facts
    else:
        assert facts["pos"] == (4, 70, 9)


def test_reflex_facts_preserve_observed_zero_health_without_position() -> None:
    class DeathSampleIpc(FakeClientIpc):
        def state(self):
            return {"health": 0, "ready": False}
    facts = RealClientBody("CapProbe2", DeathSampleIpc()).get_reflex_facts()
    assert facts["health"] == 0
    assert "pos" not in facts


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


def test_context_entities_reuse_state_without_ipc_and_clear_with_new_samples() -> None:
    class SampleIpc(FakeClientIpc):
        reads = 0
        entities = [{"type": "wither", "boss": True, "pos": [1, 70, 2]}]

        def state(self):
            self.reads += 1
            return {**super().state(), "entities": self.entities}

    ipc = SampleIpc()
    body = RealClientBody("CapProbe2", ipc)
    assert body.get_context_entities() == ()
    state = body.get_state()
    reads = ipc.reads
    entities = body.get_context_entities()
    assert ipc.reads == reads
    assert entities[0]["type"] == "wither"
    entities[0]["pos"][0] = 999
    assert body.get_context_entities()[0]["pos"] == [1, 70, 2]
    assert not hasattr(state, "nearby_entities")
    ipc.entities = None  # Older/missing samples must not retain a previous boss.
    body.get_state()
    assert body.get_context_entities() == ()


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


@pytest.mark.parametrize("error, retryable, holds_slot", [
    (RealClientNotAccepted("invalid_fixture_size"), False, False),
    (RealClientNotAccepted("body_busy"), True, False),
    (RealClientIpcError("action timed out: build-1"), True, True),
])
def test_a_build_the_client_never_started_frees_the_slot(error, retryable, holds_slot) -> None:
    """Villa run 2026-10-16: a 600-cell build refused before it started was
    reported as a transport error, and 41 later actions said body_busy."""
    class RefusingIpc(FakeClientIpc):
        def command(self, objective, **kwargs):
            raise error
    body = RealClientBody("CapProbe2", RefusingIpc())  # type: ignore[arg-type]
    build = Action("build-1", "buildStructure", {"origin": [8, 70, 7], "size": [30, 1, 20], "block": "oak_planks"})
    assert body.execute(build).accepted
    terminal = body.await_action_terminal(build.id, timeout_s=1.0)

    assert terminal.data["reason"] == (str(error) if isinstance(error, RealClientRejected) else "transport:RealClientIpcError")
    assert terminal.data["control_unknown"] is holds_slot
    assert terminal.data.get("retryable", True) is retryable
    following = body.execute(Action("move-2", "navigate", {"goal": {"kind": "xz", "x": 10, "z": 20}}))
    assert following.accepted is not holds_slot


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


def test_a_restored_connection_reports_the_world_back() -> None:
    """Review 2026-10-17: a client still in its world only replays the
    world_ready already seen, so the Body loop stayed stopped after a reconnect."""
    class ReadyIpc(FakeClientIpc):
        def wait_ready(self, timeout_s=None):
            return {"type": "ready"}

    body = RealClientBody("CapProbe2", ReadyIpc())  # type: ignore[arg-type]
    seen = []
    body.add_lifecycle_listener(seen.append)
    body._ensure_connected()  # the first handshake of a session is not news
    body._on_lifecycle_event({"name": "world_disconnected", "epoch": "python-ipc", "seq": 1,
                              "data": {"reason": "client_connection_lost", "source": "python_ipc"}})
    body._ensure_connected()
    body._ensure_connected()
    assert [event.name for event in seen] == ["world_disconnected", "world_ready"]
    assert seen[-1].data["reason"] == "client_connection_restored"


def test_real_client_entities_are_measured_to_the_nearest_hit_box() -> None:
    # End run-6: the dragon 44-51 blocks off by its parts (61-70 by its centre)
    # was offered to shoot, then not found within 64.
    class DragonIpc(FakeClientIpc):
        def state(self, *args, **kwargs):
            payload = dict(super().state(*args, **kwargs))
            payload["entities"] = [
                {"id": "d", "name": "Ender Dragon", "type": "ender_dragon", "dist2": 66.0 ** 2, "nearest_dist": 45.0},
                {"id": "e", "name": "Enderman", "type": "enderman", "dist2": 50.0 ** 2, "nearest_dist": 49.8},
                {"id": "c", "name": "End Crystal", "type": "end_crystal", "dist2": 70.0 ** 2},
            ]
            return payload
    body = RealClientBody("CapProbe2", DragonIpc())  # type: ignore[arg-type]
    seen = body.perceive("nearbyEntities", {"radius": 64, "limit": 8})
    assert [item["id"] for item in seen.data["entities"]] == ["d", "e"]


def test_real_client_entities_are_matched_before_the_nearest_are_kept() -> None:
    # End run-7, 22:27: the 64 nearest were endermen and arrows; the dragon
    # perched 15 blocks off was "not found".
    class CrowdIpc(FakeClientIpc):
        def state(self, *args, **kwargs):
            payload = dict(super().state(*args, **kwargs))
            payload["entities"] = [{"id": f"m{i}", "name": "Enderman", "type": "enderman", "dist2": 4.0}
                                   for i in range(70)] + [
                {"id": "d", "name": "Ender Dragon", "type": "minecraft:ender_dragon", "dist2": 15.0 ** 2}]
            return payload
    body = RealClientBody("CapProbe3", CrowdIpc())  # type: ignore[arg-type]
    assert "d" not in [item["id"] for item in body.perceive("nearbyEntities", {"radius": 64, "limit": 64}).data["entities"]]
    for match in ("ender_dragon", "Ender Dragon", "d"):
        seen = body.perceive("nearbyEntities", {"radius": 64, "limit": 64, "match": match})
        assert [item["id"] for item in seen.data["entities"]] == ["d"]


def _real_client_registry(ipc: FakeClientIpc, provider: str = "real_client"):
    runtime = build_body_provider(
        "real_client",
        bot_name="CapProbe2",
        natural_region=REGION,
        real_client_ipc=ipc,  # type: ignore[arg-type]
    )
    return build_phase1_registry(
        runtime.body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider=provider,
            governance_policy=runtime.governance,
        ),
    )


def test_walking_tools_offer_look_down_and_default_walks_level() -> None:
    ipc = FakeClientIpc()
    registry = _real_client_registry(ipc)

    for name in WALKING_TOOL_NAMES:
        properties = registry.get(name).input_schema["properties"]
        assert properties["look_down"]["type"] == "boolean", name
    assert "look_down" not in registry.get("mine_block_collect").input_schema["properties"]

    result = registry.get("move_to").callable({"pos": [10, 70, 9], "timeout_s": 1})
    assert result.success
    assert ipc.look_down == [False]


def test_move_to_with_look_down_reaches_the_client_command() -> None:
    ipc = FakeClientIpc()
    registry = _real_client_registry(ipc)

    # A plan node calls the same registered tool with its arguments unchanged.
    result = registry.get("move_to").callable({"pos": [10, 70, 9], "timeout_s": 1, "look_down": True})

    assert result.success
    assert ipc.commands[0][0] == "near:10:70:9:0"
    assert ipc.look_down == [True]
    # The scope ends with the call: the next walk is level again.
    registry.get("move_to").callable({"pos": [10, 70, 9], "timeout_s": 1, "look_down": False})
    assert ipc.look_down == [True, False]


def test_look_down_scope_marks_objectives_and_structured_actions_only() -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]

    with walk_look_down_scope(True):
        body.execute(Action("ld-walk", "navigate", {"goal": {"kind": "near", "x": 10, "y": 70, "z": 9, "range": 1}}))
        body.await_action_terminal("ld-walk", timeout_s=1.0)
        body.execute(Action("ld-up", "ascend", {"timeout_ticks": 20}))
        body.await_action_terminal("ld-up", timeout_s=1.0)
    body.execute(Action("ld-up2", "ascend", {"timeout_ticks": 20}))
    body.await_action_terminal("ld-up2", timeout_s=1.0)

    assert ipc.look_down == [True]
    assert ipc.actions[0][1]["look_down"] is True
    assert "look_down" not in ipc.actions[1][1]


def test_other_providers_do_not_offer_look_down() -> None:
    from unittest.mock import Mock

    body = Mock()
    body.bot_name = "Bot1"
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION))
    assert "look_down" not in registry.get("move_to").input_schema["properties"]


class MovementIpc(FakeClientIpc):
    """setMovement/setGaze are settings the client takes at once, even while an action runs."""

    def __init__(self) -> None:
        super().__init__()
        self.movement = {"mode": "stand"}

    def action(self, name, params, *, action_id, timeout_ms, on_progress=None):
        self.actions.append((name, dict(params), action_id, timeout_ms))
        if name == "setMovement":
            self.movement = ({"mode": "stand"} if params["mode"] == "stand" else
                             {"mode": "keep_moving", "center": params.get("center", [4.5, 70.0, 9.5]),
                              "radius": params["radius"], "sprint": params["sprint"]})
            return RealClientTerminal(action_id, "completed", "movement_set",
                                      {"position": [4.5, 70.0, 9.5], "movement": dict(self.movement)})
        return RealClientTerminal(action_id, "completed", "gaze_set" if name == "setGaze" else "verified",
                                  {"position": [4.5, 70.0, 9.5], "action_kind": name})

    def state(self):
        return {**super().state(), "movement": dict(self.movement)}


def test_set_movement_is_a_leg_setting_held_by_the_client() -> None:
    """body-channels-20261105 §6.2: keep_moving is a mode the client holds, not a looping call."""
    ipc = MovementIpc()
    registry, body = _real_client_registry_and_body(ipc)
    assert not _has_tool(registry, "keep_moving")
    tool = registry.get("set_movement")
    assert not tool.sidecar.mutating and tool.sidecar.timeout_s == 10

    on = tool.callable({"mode": "keep_moving", "radius": 10, "reason": "the dragon fight"})
    assert on.success and on.reason == "movement_set"
    assert on.metrics["movement"] == "keeps moving around [4, 70, 10] within 10 blocks (your setting)"
    assert ipc.actions[-1][0] == "setMovement"
    assert ipc.actions[-1][1] == {"mode": "keep_moving", "radius": 10.0, "sprint": True}
    assert body.get_state().movement["mode"] == "keep_moving"

    off = tool.callable({"mode": "stand"})
    assert off.success and off.metrics["movement"] == "standing (your setting)"
    assert ipc.actions[-1][1] == {"mode": "stand"}


def test_a_death_that_ended_keep_moving_stays_said_until_the_agent_sets_the_legs() -> None:
    ipc = MovementIpc()
    registry, body = _real_client_registry_and_body(ipc)
    body._on_lifecycle_event({"name": "death", "epoch": "e", "seq": 1, "data": {"movement_reset": True}})
    assert body.get_state().movement == {"mode": "stand", "reset_by_death": True}
    registry.get("set_movement").callable({"mode": "stand"})
    assert body.get_state().movement == {"mode": "stand"}


def test_movement_ended_reaches_the_lifecycle_listeners() -> None:
    ipc = MovementIpc()
    _registry, body = _real_client_registry_and_body(ipc)
    heard = []
    body.add_lifecycle_listener(heard.append)
    body._on_lifecycle_event({"name": "movement_ended", "epoch": "e", "seq": 2,
                              "data": {"reason": "cannot_move", "position": [1.0, 64.0, 2.0]}})
    assert [event.name for event in heard] == ["movement_ended"]


def test_walk_gaze_setting_reaches_the_client_but_a_death_reset_does_not_send_it() -> None:
    ipc = MovementIpc()
    _registry, body = _real_client_registry_and_body(ipc)
    body.set_walk_look_down(True, cause="agent")
    assert ipc.actions[-1][:2] == ("setGaze", {"look_down": True})
    count = len(ipc.actions)
    body.set_walk_look_down(False, cause="reset_on_death")
    assert len(ipc.actions) == count


def _has_tool(registry, name) -> bool:
    try:
        registry.get(name)
        return True
    except Exception:
        return False


def test_ranged_attack_ends_at_release_and_points_to_last_shot() -> None:
    target_uuid = "11111111-1111-4111-8111-111111111111"

    class ReleaseIpc(FakeClientIpc):
        def action(self, name, params, *, action_id, timeout_ms, **kwargs):
            super().action(name, params, action_id=action_id, timeout_ms=timeout_ms, **kwargs)
            return RealClientTerminal(action_id, "completed", "arrow_released", {
                "target_id": params["target_uuid"], "shots": 1,
                "last_shot": {"target_id": params["target_uuid"], "outcome": "in_flight", "age_s": 0.1}})

    ipc = ReleaseIpc()
    body = RealClientBody("CapProbe2", ipc)  # type: ignore[arg-type]
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client"))
    result = registry.get("ranged_attack").callable({"target": "Sheep", "timeout_s": 1})
    assert result.success and result.reason == "arrow_released"
    assert "last_shot" in (result.next_suggestion or "")
    assert ipc.actions[0][1]["target_uuid"] == target_uuid


def test_read_state_carries_the_last_shot() -> None:
    class ShotIpc(FakeClientIpc):
        def state(self):
            return {**super().state(), "last_shot": {"target_type": "end_crystal", "outcome": "hit",
                                                      "target_gone": True, "age_s": 1.2}}

    body = RealClientBody("CapProbe2", ShotIpc())  # type: ignore[arg-type]
    registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client"))
    result = registry.get("read_state").callable({})
    assert result.metrics["last_shot"]["outcome"] == "hit"


def _real_client_registry_and_body(ipc: FakeClientIpc):
    runtime = build_body_provider(
        "real_client", bot_name="CapProbe2", natural_region=REGION,
        real_client_ipc=ipc,  # type: ignore[arg-type]
    )
    registry = build_phase1_registry(
        runtime.body,
        Phase1RuntimeConfig(natural_region=REGION, body_provider="real_client",
                            governance_policy=runtime.governance),
    )
    return registry, runtime.body


def test_set_walk_gaze_keeps_walking_tools_looking_down_until_turned_off() -> None:
    from minebot.app.client_body_tools import SET_WALK_GAZE_DESCRIPTION

    ipc = FakeClientIpc()
    registry, body = _real_client_registry_and_body(ipc)
    tool = registry.get("set_walk_gaze")
    assert tool.description == SET_WALK_GAZE_DESCRIPTION == (
        "Keep every walk of the Body looking at the ground (your calls, plans and Decision's own moves) "
        "until you turn it off or the Body dies; use it where looking around is risky, e.g. endermen in the End.")
    assert tool.input_schema["required"] == ["look_down"]

    on = tool.callable({"look_down": True, "reason": "endermen around"})
    assert on.success and on.metrics == {"walking": "looking down (your setting)", "changed": True}
    assert body.walk_look_down is True and body.get_state().walk_look_down is True
    # A walking tool called without the argument walks looking down.
    registry.get("move_to").callable({"pos": [10, 70, 9], "timeout_s": 1})
    # The caller's explicit false still wins.
    registry.get("move_to").callable({"pos": [10, 70, 9], "timeout_s": 1, "look_down": False})
    assert ipc.look_down == [True, False]

    off = tool.callable({"look_down": False})
    assert off.metrics == {"walking": "level", "changed": True}
    registry.get("move_to").callable({"pos": [10, 70, 9], "timeout_s": 1})
    assert ipc.look_down == [True, False, False]
    assert body.get_state().walk_look_down is None
    assert "walk_look_down" not in body.get_reflex_facts()

    # A death turns it off: STATE says so until the Agent sets the gaze again.
    body.set_walk_look_down(True, cause="agent")
    body.set_walk_look_down(False, cause="reset_on_death")
    assert body.get_state().walk_look_down is False
    assert "walk_look_down" not in body.get_reflex_facts()
    body.set_walk_look_down(False, cause="agent")
    assert body.get_state().walk_look_down is None
    # A death while it was already off says nothing.
    body.set_walk_look_down(False, cause="reset_on_death")
    assert body.get_state().walk_look_down is None


def test_standing_walk_gaze_reaches_decisions_own_moves_but_not_aimed_actions() -> None:
    ipc = FakeClientIpc()
    registry, body = _real_client_registry_and_body(ipc)
    body.set_walk_look_down(True, cause="agent")

    # Decision's step option: the internal reflex step tool, outside any walking tool.
    step = registry.get("__reflex_step").callable({"pos": [4, 70, 6], "timeout_s": 1})
    assert step.success
    assert ipc.actions[-1][0] == "stepTo" and ipc.actions[-1][1]["look_down"] is True
    # Decision's move_away / a plan's walk: a navigate action outside a walking tool.
    body.execute(Action("ld-away", "navigate", {"goal": {"kind": "near", "x": 10, "y": 70, "z": 9, "range": 1}}))
    body.await_action_terminal("ld-away", timeout_s=1.0)
    assert ipc.look_down == [True]
    # An aimed use outside a walking tool keeps its own aim.
    body.execute(Action("ld-use", "useItem", {"item": "ender_pearl", "mode": "once", "timeout_ticks": 20}))
    body.await_action_terminal("ld-use", timeout_s=1.0)
    assert "look_down" not in ipc.actions[-1][1]
    # Off: Decision's next step looks level again.
    body.set_walk_look_down(False, cause="agent")
    registry.get("__reflex_step").callable({"pos": [4, 70, 6], "timeout_s": 1})
    assert "look_down" not in ipc.actions[-1][1]


class WalkingLegsIpc(FakeClientIpc):
    """No action runs; Baritone paths because the keep_moving legs walk."""

    def __init__(self, *, held: bool = False) -> None:
        super().__init__()
        self.held = held

    def state(self):
        return {**super().state(), "active_action_known": True, "active_action_id": None,
                "any_process_active": True, "pathing": True,
                "movement": {"mode": "keep_moving", "center": [4, 70, 10], "radius": 10.0,
                             "held_by_action": self.held}}

    def control(self, kind, **kwargs):
        self.controls.append((kind, dict(kwargs)))
        return self.state()


def test_walking_legs_are_no_running_action() -> None:
    """End run-20: the legs' pathing read as a running action failed 2148 reflex calls."""
    ipc = WalkingLegsIpc()
    body = RealClientBody("Legs", ipc)  # type: ignore[arg-type]
    assert body.confirm_control_idle()["native_stopped"] is True
    result = body.interrupt("threat")
    assert ipc.controls == [] and result.error == "no_action_active"


def test_pathing_of_a_paused_walk_still_counts() -> None:
    """Legs paused under a leg action: the pathing is that action's, so control is not idle."""
    ipc = WalkingLegsIpc(held=True)
    body = RealClientBody("Legs", ipc)  # type: ignore[arg-type]
    assert body.confirm_control_idle()["native_stopped"] is False

