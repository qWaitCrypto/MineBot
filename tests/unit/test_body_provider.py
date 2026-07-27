from __future__ import annotations

from unittest.mock import Mock

import pytest

from minebot.app.body_provider import (
    BodyProviderConfigError,
    BodyProviderName,
    build_body_provider,
)
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Region
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
