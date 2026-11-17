from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from minebot.app.body_provider import (
    BodyProviderConfigError,
    BodyProviderName,
    build_body_provider,
)
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.body import (
    BlockApproachTransactions,
    BlockWork,
    ContainerTransactions,
    ExplorationTransactions,
    FurnaceTransactions,
    InteractionTransactions,
    InventoryTransactions,
    PickupTransactions,
    ResourceCollectionTransactions,
    UseTransactions,
)
from minebot.contract import Action, BreakContext, PerceptionResult, Region
from minebot.game.composite_body import CompositeBody
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import GovernanceAnswerer, JavaBodyClient

from tests.unit.test_java_body_adapter import FakeBodyServer


REGION = Region("test", (-64, -64, -64), (64, 320, 64))


class ResourceOrchestrationServer(FakeBodyServer):
    """Small world fixture for Python-owned resource orchestration."""

    def __init__(self, resources: dict[tuple[int, int, int], str]) -> None:
        super().__init__()
        self.resources = {
            pos: block if block.startswith("minecraft:") else f"minecraft:{block}"
            for pos, block in resources.items()
        }

    def _handle_find_blocks(self, message: dict, rid: str) -> None:
        wanted = set(message.get("block_ids") or ())
        matches = [
            {
                "x": pos[0],
                "y": pos[1],
                "z": pos[2],
                "block_id": block,
                "state": "SOLID",
                "distance_squared": float(
                    (pos[0] - 10) ** 2 + (pos[1] - 64) ** 2 + (pos[2] + 4) ** 2
                ),
            }
            for pos, block in self.resources.items()
            if block in wanted
        ]
        matches.sort(key=lambda item: (item["distance_squared"], item["x"], item["y"], item["z"]))
        self._emit_response(rid, "FIND_BLOCKS_RESULT", {
            "server_cost_micros": 50,
            "start": 0,
            "total_matches": len(matches),
            "index_generation": 1,
            "coverage_complete": True,
            "unloaded_chunk_count": 0,
            "result_capped": False,
            "next_cursor": None,
            "matches": matches,
        })

    def _handle_navigate(self, message: dict, rid: str) -> None:
        goal = dict(message["goal"])
        while goal.get("kind") == "composite":
            goal = dict((goal.get("goals") or [])[0])
        if goal.get("kind") == "xz":
            x, y, z = int(goal["x"]), int(self.position[1]), int(goal["z"])
        else:
            x, y, z = int(goal["x"]), int(goal["y"]), int(goal["z"])
        self.position = [x + 0.5, float(y), z + 0.5]
        action = message["action_id"]
        self._emit_response(rid, "NAVIGATE_ACK", {
            "action_id": action,
            "state": "accepted",
        })
        self._emit_event("owner_acquired", action, {
            "type": "NAVIGATE",
            "priority": "ACTION",
        })
        self._emit_terminal(action, {
            "classification": "completed",
            "reason": "goal_satisfied",
            "elapsed_ticks": 8,
            "replans": 0,
            "final_x": self.position[0],
            "final_y": self.position[1],
            "final_z": self.position[2],
        })

    def _handle_world_read(self, message: dict, rid: str) -> None:
        scope = message["scope"]
        params = message.get("params") or {}
        if scope not in {"blockAt", "blockCells"}:
            super()._handle_world_read(message, rid)
            return

        def block_fact(pos: tuple[int, int, int]) -> dict:
            resource = self.resources.get(pos)
            if resource is not None:
                return {
                    "x": pos[0], "y": pos[1], "z": pos[2],
                    "type": resource, "state": "SOLID", "properties": {},
                }
            if pos in {(10, 64, -4), (10, 65, -4)}:
                return {
                    "x": pos[0], "y": pos[1], "z": pos[2],
                    "type": "minecraft:air", "state": "CLEAR", "properties": {},
                }
            return {
                "x": pos[0], "y": pos[1], "z": pos[2],
                "type": "minecraft:stone", "state": "SOLID", "properties": {},
            }

        if scope == "blockAt":
            pos = (int(params["x"]), int(params["y"]), int(params["z"]))
            data: dict = block_fact(pos)
        else:
            requested = [tuple(int(value) for value in pos) for pos in params.get("cells") or ()]
            data = {
                "start": 0,
                "limit": len(requested),
                "nextStart": None,
                "total": len(requested),
                "count": len(requested),
                "cells": [block_fact(pos) for pos in requested],
            }
        if params.get("include_standing") is True:
            for fact in [data] if scope == "blockAt" else data["cells"]:
                pos = (fact["x"], fact["y"], fact["z"])
                fact["standing_y"] = 64.0 if pos == (10, 64, -4) else None
        if scope == "blockAt" and params.get("interaction_radius") is not None:
            # The Body answers its live reach predicate when asked for one.
            data["interaction_ready"] = True
        self._emit_response(rid, "WORLD_READ_RESULT", {
            "bot": message.get("bot_name"),
            "scope": scope,
            "missing": False,
            "ok": True,
            "complete": True,
            "server_cost_micros": 25,
            "data": data,
            "uncertainty": [],
            "next": None,
        })

    def _handle_verdict(self, message: dict) -> None:
        target = tuple((getattr(self, "_collect_request", {}) or {}).get("target") or ())
        super()._handle_verdict(message)
        if bool(message.get("allow")) and len(target) == 3:
            self.resources.pop(target, None)


def _scarpet() -> Mock:
    body = Mock()
    body.bot_name = "Bot"
    return body


def test_default_provider_returns_the_existing_scarpet_body() -> None:
    scarpet = _scarpet()

    runtime = build_body_provider(
        BodyProviderName.SCARPET,
        bot_name="Bot",
        natural_region=REGION,
        scarpet_body=scarpet,
    )

    assert runtime.body is scarpet
    assert runtime.java_body is None


def test_composite_constructs_java_body_without_connecting_at_startup() -> None:
    server = FakeBodyServer()

    runtime = build_body_provider(
        "composite",
        bot_name="Bot",
        natural_region=REGION,
        scarpet_body=_scarpet(),
        java_connect=lambda: server,
    )

    assert isinstance(runtime.body, CompositeBody)
    assert runtime.java_body is not None
    assert runtime.java_body._client.negotiated is False
    assert runtime.java_body._read_client is not runtime.java_body._client
    assert runtime.java_body._client._governance is not None
    assert runtime.java_body._read_client._governance is runtime.java_body._client._governance


def test_java_read_lane_does_not_subscribe_to_live_action_events() -> None:
    server = FakeBodyServer()
    runtime = build_body_provider(
        "java",
        bot_name="Bot",
        natural_region=REGION,
        java_connect=lambda: server,
    )
    assert runtime.java_body is not None

    runtime.java_body.get_state()
    runtime.java_body.event_head("subscription-test")
    runtime.java_body.poll_events()

    hellos = [request for request in server.requests if request.get("type") == "HELLO"]
    assert len(hellos) == 2
    assert hellos[0]["subscribe_events"] is False
    assert "subscribe_events" not in hellos[1]
    assert runtime.java_body._client._reconciliation_client is runtime.java_body._read_client


def test_java_backed_providers_enable_the_single_java_survival_owner() -> None:
    java_server = FakeBodyServer()
    java_runtime = build_body_provider(
        "java",
        bot_name="Bot",
        natural_region=REGION,
        java_connect=lambda: java_server,
    )
    assert java_runtime.java_body is not None
    java_runtime.java_body.event_head("java-provider-test")
    java_runtime.body.get_state()
    java_runtime.java_body.poll_events()

    composite_server = FakeBodyServer()
    composite_runtime = build_body_provider(
        "composite",
        bot_name="Bot",
        natural_region=REGION,
        scarpet_body=_scarpet(),
        java_connect=lambda: composite_server,
    )
    assert composite_runtime.java_body is not None
    composite_runtime.java_body.event_head("composite-provider-test")
    composite_runtime.java_body.get_state()
    composite_runtime.java_body.poll_events()

    assert next(
        request for request in java_server.requests
        if request["type"] == "SET_SURVIVAL_OWNER"
    )["enabled"] is True
    assert next(
        request for request in composite_server.requests
        if request["type"] == "SET_SURVIVAL_OWNER"
    )["enabled"] is True


@pytest.mark.parametrize("scope", sorted(CompositeBody.JAVA_PERCEPTIONS))
def test_composite_routes_migrated_perceptions_to_java_without_runtime_fallback(
    scope: str,
) -> None:
    scarpet = _scarpet()
    java = _scarpet()
    java_result = PerceptionResult(
        bot="Bot",
        scope=scope,
        type="perception",
        ok=False,
        complete=False,
        error="inventory_internal_error",
    )
    java.perceive.return_value = java_result
    body = CompositeBody(scarpet, java)

    params = {"start": 0, "limit": 12}
    result = body.perceive(scope, params)

    assert result is java_result
    java.perceive.assert_called_once_with(scope, params)
    scarpet.perceive.assert_not_called()


@pytest.mark.parametrize("action_name", sorted(CompositeBody.JAVA_TERMINAL_ACTIONS))
def test_composite_routes_migrated_player_action_and_its_terminal_to_java(
    action_name: str,
) -> None:
    scarpet = _scarpet()
    java = JavaBody(JavaBodyClient("Bot", lambda: FakeBodyServer()), "Bot")
    body = CompositeBody(scarpet, java)
    params = {
        "engageEntity": {
            "target_uuid": "11111111-1111-4111-8111-111111111111",
            "attack_range": 2.0,
            "cooldown_ticks": 10,
            "acquire_radius": 32,
            "timeout_ticks": 100,
            "disengage_health": 6.0,
        },
        "followEntity": {
            "target_uuid": "22222222-2222-4222-8222-222222222222",
            "keep_radius": 3.0,
            "replan_distance": 2.0,
            "acquire_radius": 32,
            "timeout_ticks": 100,
        },
        "containerTransfer": {
            "pos": [1, 64, 0],
            "direction": "container_to_bot",
            "container_slot": 0,
            "bot_slot": 1,
            "count": 2,
        },
        "craftItem": {
            "inputs": [{"slot": 0, "item": "minecraft:oak_log", "count": 1}],
            "output": {"slot": 1, "item": "minecraft:oak_planks", "count": 4},
            "remainders": [],
            "max_stack": 64,
        },
        "furnaceTransfer": {
            "pos": [1, 64, 0],
            "direction": "bot_to_furnace",
            "furnace_slot": "input",
            "bot_slot": 5,
            "count": 2,
        },
        "dropItem": {"slot": 0, "mode": "one"},
        "handoffItem": {
            "receiver": "MineBotGuide",
            "item": "minecraft:diamond",
            "count": 2,
            "timeout_ticks": 60,
        },
        "igniteBlock": {
            "target": [1, 64, 0],
            "item": "minecraft:flint_and_steel",
            "allow_server_substitute": True,
        },
        "jump": {},
        "mineBlock": {"target": [1, 64, 0], "block_type": "minecraft:stone", "context": "direct"},
        "placeBlock": {"target": [1, 64, 0], "block_type": "minecraft:cobblestone", "context": "work"},
        "selectItem": {"item": "minecraft:bread"},
        "lookAt": {"target": [1.0, 65.0, 1.0]},
        "moveItem": {"from_slot": 18, "to_slot": 0, "count": 3},
        "stop": {},
        "sowCrop": {
            "target": [2, 64, 0],
            "crop_block": "minecraft:wheat",
            "seed_item": "minecraft:wheat_seeds",
            "allow_server_substitute": True,
        },
        "useItem": {"item": "minecraft:bread", "ticks": 2},
    }[action_name]
    action = Action.create(action_name, params)

    accepted = body.execute(action)
    terminal = body.await_action_terminal(action.id)

    assert accepted.ok and accepted.accepted
    assert terminal.bot == "Bot"
    assert terminal.data["action_id"] == action.id
    scarpet.execute.assert_not_called()
    scarpet.await_action_terminal.assert_not_called()


def test_composite_does_not_fallback_after_java_player_action_failure() -> None:
    server = FakeBodyServer()
    server.scenario = "player_action_no_effect"
    scarpet = _scarpet()
    body = CompositeBody(
        scarpet,
        JavaBody(JavaBodyClient("Bot", lambda: server), "Bot"),
    )
    action = Action.create("useItem", {"item": "minecraft:bread", "ticks": 2})

    accepted = body.execute(action)
    terminal = body.await_action_terminal(action.id)

    assert accepted.ok and accepted.accepted
    assert terminal.data["success"] is False
    assert terminal.data["stopped_reason"] == "no_effect"
    scarpet.execute.assert_not_called()
    scarpet.await_action_terminal.assert_not_called()


def test_composite_routes_lifecycle_and_interrupt_to_java_only() -> None:
    scarpet = _scarpet()
    java = _scarpet()
    java.spawn.return_value = Mock(ok=True, accepted=True)
    java.despawn.return_value = Mock(ok=True, accepted=True)
    java.interrupt.return_value = Mock(
        ok=True, accepted=True, complete=True, data={"owner": None}, error=None
    )
    scarpet.interrupt.return_value = Mock(
        ok=True, accepted=True, complete=True, data={"owner": None}, error=None
    )
    body = CompositeBody(scarpet, java)

    body.spawn((3, 59, 0), emit_respawned=True, timeout_s=2.0)
    body.despawn()
    interrupted = body.interrupt("cleanup")

    java.spawn.assert_called_once_with((3, 59, 0), emit_respawned=True, timeout_s=2.0)
    java.despawn.assert_called_once_with()
    scarpet.spawn.assert_not_called()
    scarpet.despawn.assert_not_called()
    java.interrupt.assert_called_once_with("cleanup")
    scarpet.interrupt.assert_not_called()
    assert interrupted.ok and interrupted.accepted and interrupted.complete


def test_composite_state_and_event_stream_are_java_owned() -> None:
    scarpet = _scarpet()
    java = _scarpet()
    java.event_head.return_value = {
        "event_seq": 11,
        "chat_seq": 0,
        "epoch": "java-epoch",
        "owner": "nav-1",
        "pending_action_count": 1,
    }
    java.get_state.return_value = object()
    java.poll_events.return_value = []
    java.poll_chat_events.return_value = []
    java.world_identity.return_value = "world-java"
    java.say.return_value = True

    body = CompositeBody(scarpet, java)
    head = body.event_head("client-epoch")
    state = body.get_state()
    events = body.poll_events()
    chat_events = body.poll_chat_events()
    world_id = body.world_identity()
    said = body.say("hello")

    assert head["event_seq"] == 11
    assert head["chat_seq"] == 0
    assert head["epoch"] == "java-epoch"
    assert head["owner"] == "nav-1"
    assert head["pending_action_count"] == 1
    assert state is java.get_state.return_value
    assert events == []
    assert chat_events == []
    assert world_id == "world-java"
    assert said is True
    scarpet.event_head.assert_not_called()
    scarpet.get_state.assert_not_called()
    scarpet.poll_events.assert_not_called()
    scarpet.poll_chat_events.assert_not_called()
    scarpet.world_identity.assert_not_called()
    scarpet.say.assert_not_called()


def test_composite_unknown_capability_returns_java_gap_without_scarpet_fallback() -> None:
    scarpet = _scarpet()
    java = JavaBody(JavaBodyClient("Bot", lambda: FakeBodyServer()), "Bot")
    body = CompositeBody(scarpet, java)

    perception = body.perceive("legacyOnlyRead", {})
    action = body.execute(Action.create("legacyOnlyAction", {}))

    assert perception.ok is False
    assert perception.error == "capability_unavailable:legacyOnlyRead"
    assert action.ok is False
    assert action.error == "capability_unavailable:legacyOnlyAction"
    scarpet.perceive.assert_not_called()
    scarpet.execute.assert_not_called()


def test_composite_governance_structure_read_uses_java_world_facts() -> None:
    server = FakeBodyServer()
    scarpet = _scarpet()
    runtime = build_body_provider(
        "composite",
        bot_name="Bot",
        natural_region=REGION,
        scarpet_body=scarpet,
        java_connect=lambda: server,
    )

    runtime.governance.can_break(
        (1, 64, 0),
        "minecraft:oak_log",
        BreakContext.COLLECT,
        explicit_target=True,
    )

    assert any(
        request.get("type") == "WORLD_READ" and request.get("scope") == "blockCells"
        for request in server.requests
    )
    scarpet.perceive.assert_not_called()


def test_invalid_provider_is_rejected() -> None:
    with pytest.raises(BodyProviderConfigError, match="MINEBOT_BODY_PROVIDER"):
        build_body_provider("automatic", bot_name="Bot", natural_region=REGION)


def test_java_provider_uses_canonical_move_to_tool() -> None:
    runtime = build_body_provider(
        "java",
        bot_name="Bot",
        natural_region=REGION,
        java_connect=lambda: FakeBodyServer(),
    )
    registry = build_phase1_registry(
        runtime.body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=runtime.governance,
        ),
    )

    result = registry.get("move_to").callable({"pos": [10, 64, 0], "radius": 1})

    assert result.success is True
    assert result.reason == "arrived"
    assert "navigate_to" not in registry.names()


def test_java_provider_wires_known_target_movement_to_the_objective_navigator() -> None:
    body = _scarpet()
    objective_navigator = Mock(name="objective_navigator")
    legacy_navigator = Mock(name="legacy_navigator")

    with (
        patch(
            "minebot.app.phase1_runtime.NavigationTransactions.server_side",
            return_value=legacy_navigator,
        ),
        patch(
            "minebot.app.phase1_runtime.ObjectiveNavigationTransactions",
            return_value=objective_navigator,
        ) as objective_factory,
        patch(
            "minebot.app.phase1_runtime.PickupTransactions",
            wraps=PickupTransactions,
        ) as pickup,
        patch(
            "minebot.app.phase1_runtime.BlockWork",
            wraps=BlockWork,
        ) as work,
        patch(
            "minebot.app.phase1_runtime.ExplorationTransactions",
            wraps=ExplorationTransactions,
        ) as exploration,
        patch(
            "minebot.app.phase1_runtime.ResourceCollectionTransactions",
            wraps=ResourceCollectionTransactions,
        ) as resource_collection,
        patch(
            "minebot.app.phase1_runtime.BlockApproachTransactions",
            wraps=BlockApproachTransactions,
        ) as block_approach,
        patch(
            "minebot.app.phase1_runtime.InventoryTransactions",
            wraps=InventoryTransactions,
        ) as inventory,
        patch(
            "minebot.app.phase1_runtime.FurnaceTransactions",
            wraps=FurnaceTransactions,
        ) as furnace,
        patch(
            "minebot.app.phase1_runtime.UseTransactions",
            wraps=UseTransactions,
        ) as use,
        patch(
            "minebot.app.phase1_runtime.InteractionTransactions",
            wraps=InteractionTransactions,
        ) as interaction,
        patch(
            "minebot.app.phase1_runtime.ContainerTransactions",
            wraps=ContainerTransactions,
        ) as container,
    ):
        build_phase1_registry(
            body,
            Phase1RuntimeConfig(natural_region=REGION, body_provider="java"),
        )

    objective_factory.assert_called_once_with(body)
    assert pickup.call_args.args[1] is objective_navigator
    assert work.call_args.kwargs["navigator"] is objective_navigator
    assert exploration.call_args.args[1] is objective_navigator
    assert resource_collection.call_args.args[1] is objective_navigator
    assert block_approach.call_args.args[1] is objective_navigator
    assert inventory.call_args.kwargs["navigator"] is objective_navigator
    assert furnace.call_args.kwargs["navigator"] is objective_navigator
    assert use.call_args.kwargs["navigator"] is objective_navigator
    assert interaction.call_args.kwargs["navigator"] is objective_navigator
    assert container.call_args.kwargs["navigator"] is objective_navigator


def test_java_provider_uses_canonical_collect_domain_tool() -> None:
    policy = GovernancePolicy(natural_regions=[REGION])
    server = ResourceOrchestrationServer({(1, 64, 0): "oak_log"})
    body = JavaBody(
        JavaBodyClient(
            "Bot",
            lambda: server,
            GovernanceAnswerer(policy),
        ),
        "Bot",
    )
    registry = build_phase1_registry(
        body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=policy,
        ),
    )

    result = registry.get("collect_block_domain").callable(
        {
            "block_types": ["oak_log"],
            "expected_drops": ["oak_log"],
            "remaining_count": 1,
            "search_radius": 16,
        }
    )

    assert result.success is True
    assert result.reason == "resource_domain_collected"
    assert result.metrics["collected_total"] == 1
    assert result.metrics["remaining_delta"] == 0
    request = next(item for item in server.requests if item["type"] == "COLLECT_BLOCK")
    assert request["timeout_ticks"] == 300
    assert request["contract_version"] == 2
    assert request["target"] == [1, 64, 0]
    assert request["expected_block_id"] == "minecraft:oak_log"
    assert request["expected_item_ids"] == ["minecraft:oak_log"]
    assert all(
        field not in request
        for field in ("block_ids", "radius", "target_count", "candidate_budget")
    )
    assert "collect_block" not in registry.names()


def test_java_collect_quantity_is_python_loop_over_designated_actions() -> None:
    policy = GovernancePolicy(natural_regions=[REGION])
    server = ResourceOrchestrationServer({
        (1, 64, 0): "iron_ore",
        (4, 64, 0): "iron_ore",
        (7, 64, 0): "deepslate_iron_ore",
    })
    body = JavaBody(
        JavaBodyClient("Bot", lambda: server, GovernanceAnswerer(policy)),
        "Bot",
    )
    registry = build_phase1_registry(
        body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=policy,
        ),
    )

    result = registry.get("collect_block_domain").callable(
        {
            "block_types": ["iron_ore", "deepslate_iron_ore"],
            "expected_drops": ["raw_iron"],
            "remaining_count": 3,
            "search_radius": 24,
            "candidate_budget": 7,
            "mutation_budget": 6,
            "max_wall_s": 90,
        }
    )

    requests = [item for item in server.requests if item["type"] == "COLLECT_BLOCK"]
    assert len(requests) == 3
    assert {tuple(request["target"]) for request in requests} == {
        (1, 64, 0), (4, 64, 0), (7, 64, 0),
    }
    assert all(20 <= request["timeout_ticks"] <= 1_200 for request in requests)
    assert all(
        field not in request
        for request in requests
        for field in ("block_ids", "radius", "target_count", "candidate_budget")
    )
    assert result.success is True
    assert result.reason == "resource_domain_collected"
    assert result.metrics["collected_total"] == 3
    assert result.metrics["remaining_delta"] == 0
    assert result.metrics["candidate_attempts_consumed"] == 3


def test_java_provider_keeps_block_targets_separate_from_expected_drops() -> None:
    policy = GovernancePolicy(natural_regions=[REGION])
    server = ResourceOrchestrationServer({(1, 64, 0): "iron_ore"})
    body = JavaBody(
        JavaBodyClient(
            "Bot",
            lambda: server,
            GovernanceAnswerer(policy),
        ),
        "Bot",
    )
    registry = build_phase1_registry(
        body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=policy,
        ),
    )

    result = registry.get("collect_block_domain").callable(
        {
            "block_types": ["iron_ore", "deepslate_iron_ore"],
            "expected_drops": ["raw_iron"],
            "remaining_count": 1,
            "search_radius": 16,
        }
    )

    assert result.success is True
    request = next(item for item in server.requests if item["type"] == "COLLECT_BLOCK")
    assert request["target"] == [1, 64, 0]
    assert request["expected_block_id"] == "minecraft:iron_ore"
    assert request["expected_item_ids"] == ["minecraft:raw_iron"]


def test_java_body_does_not_replace_explicit_empty_expected_items() -> None:
    policy = GovernancePolicy(natural_regions=[REGION])
    server = FakeBodyServer()
    body = JavaBody(
        JavaBodyClient("Bot", lambda: server, GovernanceAnswerer(policy)),
        "Bot",
    )

    body.execute(
        Action.create(
            "collectBlock",
            {
                "target": [1, 64, 0],
                "expected_block_id": "minecraft:iron_ore",
                "expected_item_ids": [],
            },
        )
    )

    request = next(item for item in server.requests if item["type"] == "COLLECT_BLOCK")
    assert request["expected_item_ids"] == []


def test_java_provider_uses_canonical_go_to_surface_tool() -> None:
    server = FakeBodyServer()
    runtime = build_body_provider(
        "java",
        bot_name="Bot",
        natural_region=REGION,
        java_connect=lambda: server,
    )
    registry = build_phase1_registry(
        runtime.body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=runtime.governance,
        ),
    )

    tool = registry.get("go_to_surface")
    result = tool.callable({"timeout_s": 17})

    assert result.success is True
    assert result.reason == "surface_reached"
    assert result.metrics["final_y"] == 70
    assert tool.input_schema["properties"] == {
        "timeout_s": {"type": "number", "exclusiveMinimum": 0}
    }
    request = next(item for item in server.requests if item["type"] == "ASCEND")
    assert request["timeout_ticks"] == 340
    assert "target_y" not in request
    assert registry.sidecar("go_to_surface").source == "java_body"


def test_java_provider_uses_one_dig_down_objective_without_legacy_move_to() -> None:
    server = FakeBodyServer()
    runtime = build_body_provider(
        "java",
        bot_name="Bot",
        natural_region=REGION,
        java_connect=lambda: server,
    )
    registry = build_phase1_registry(
        runtime.body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=runtime.governance,
        ),
    )

    result = registry.get("dig_down").callable(
        {"target_y": 62, "max_steps": 3, "max_clear_fall": 1}
    )

    assert result.success is True
    assert result.reason == "dig_down_target_reached"
    dig_requests = [item for item in server.requests if item["type"] == "DIG_DOWN"]
    assert len(dig_requests) == 1
    assert dig_requests[0]["target_y"] == 62
    assert dig_requests[0]["max_steps"] == 3
    assert dig_requests[0]["max_clear_fall"] == 1
    assert not any(
        item["type"] == "PLAYER_ACTION" and item.get("action") == "moveTo"
        for item in server.requests
    )


def test_java_only_provider_uses_indexed_world_truth_without_legacy_structure_reader() -> None:
    server = ResourceOrchestrationServer({(1, 64, 0): "oak_log"})
    runtime = build_body_provider(
        "java",
        bot_name="Bot",
        natural_region=REGION,
        java_connect=lambda: server,
    )
    registry = build_phase1_registry(
        runtime.body,
        Phase1RuntimeConfig(
            natural_region=REGION,
            body_provider="java",
            governance_policy=runtime.governance,
        ),
    )

    result = registry.get("collect_block_domain").callable(
        {
            "block_types": ["oak_log"],
            "expected_drops": ["oak_log"],
            "remaining_count": 1,
        }
    )

    assert result.success is True
    assert result.reason == "resource_domain_collected"
    assert server.verdict_seen == [("mp-1", True)]
