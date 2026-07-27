from __future__ import annotations

from unittest.mock import Mock

import pytest

from minebot.app.body_provider import (
    BodyProviderConfigError,
    BodyProviderName,
    build_body_provider,
)
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Action, BreakContext, PerceptionResult, Region
from minebot.game.composite_body import CompositeBody
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import GovernanceAnswerer, JavaBodyClient

from tests.unit.test_java_body_adapter import FakeBodyServer


REGION = Region("test", (-64, -64, -64), (64, 320, 64))


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
        "jump": {},
        "mineBlock": {"target": [1, 64, 0], "block_type": "minecraft:stone", "context": "direct"},
        "placeBlock": {"target": [1, 64, 0], "block_type": "minecraft:cobblestone", "context": "work"},
        "selectItem": {"item": "minecraft:bread"},
        "lookAt": {"target": [1.0, 65.0, 1.0]},
        "moveItem": {"from_slot": 18, "to_slot": 0, "count": 3},
        "stop": {},
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


def test_java_provider_uses_canonical_collect_domain_tool() -> None:
    policy = GovernancePolicy(natural_regions=[REGION])
    body = JavaBody(
        JavaBodyClient(
            "Bot",
            lambda: FakeBodyServer(),
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
    assert result.reason == "collected"
    assert result.metrics["collected_delta"] == 1
    assert "collect_block" not in registry.names()


def test_java_provider_uses_canonical_go_to_surface_tool() -> None:
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

    result = registry.get("go_to_surface").callable({})

    assert result.success is True
    assert result.reason == "surface_reached"
    assert result.metrics["final_y"] == 70
    assert registry.sidecar("go_to_surface").source == "java_body"


def test_java_only_provider_denies_collection_without_structure_read_coverage() -> None:
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

    result = registry.get("collect_block_domain").callable(
        {
            "block_types": ["oak_log"],
            "expected_drops": ["oak_log"],
            "remaining_count": 1,
        }
    )

    assert result.success is False
    assert result.reason == "candidate_targets_exhausted"
