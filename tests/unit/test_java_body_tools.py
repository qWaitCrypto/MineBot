"""The Java-Body agent tools route the shared registry to JavaBodyClient."""

from __future__ import annotations

from minebot.app.java_body_tools import register_java_body_tools
from minebot.brain.registry import ToolRegistry
from minebot.contract.governance import Region
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body_adapter import GovernanceAnswerer, JavaBodyClient

from tests.unit.test_java_body_adapter import FakeBodyServer


def _client(server, governance=None) -> JavaBodyClient:
    return JavaBodyClient("Bot", lambda: server, governance, action_wall_timeout_s=5.0, recv_timeout_s=0.01)


def _registry(client) -> ToolRegistry:
    registry = ToolRegistry()
    register_java_body_tools(registry, client)
    return registry


def test_both_tools_register_with_mutating_sidecars() -> None:
    registry = _registry(_client(FakeBodyServer()))

    assert set(registry.names()) == {"navigate_to", "collect_block"}
    for name in ("navigate_to", "collect_block"):
        sidecar = registry.sidecar(name)
        assert sidecar.mutating is True
        assert sidecar.source == "java_body"
        assert sidecar.progress_key == name


def test_framework_view_hides_the_sidecar() -> None:
    registry = _registry(_client(FakeBodyServer()))
    views = {view["name"]: view for view in registry.framework_tools()}

    assert set(views["navigate_to"]) == {"name", "description", "input_schema"}
    assert views["navigate_to"]["input_schema"]["required"] == ["x", "z"]
    assert "block_types" in views["collect_block"]["input_schema"]["properties"]


def test_navigate_to_tool_routes_to_the_client_and_returns_terminal_truth() -> None:
    client = _client(FakeBodyServer())
    client.connect()
    registry = _registry(client)

    result = registry.get("navigate_to").callable({"x": 10, "y": 64, "z": 0, "range": 1.5})

    assert result.success is True
    assert result.reason == "arrived"
    assert result.metrics["replans"] == 2


def test_navigate_to_rejects_an_invalid_goal_kind() -> None:
    client = _client(FakeBodyServer())
    client.connect()
    registry = _registry(client)

    result = registry.get("navigate_to").callable({"x": 1, "z": 2, "kind": "teleport"})

    assert result.success is False
    assert result.reason == "invalid_goal_kind"


def test_collect_block_tool_routes_through_governance_to_inventory_delta() -> None:
    policy = GovernancePolicy(natural_regions=[Region("n", (-64, 0, -64), (64, 200, 64))])
    server = FakeBodyServer()
    client = _client(server, GovernanceAnswerer(policy))
    client.connect()
    registry = _registry(client)

    result = registry.get("collect_block").callable({"block_types": ["minecraft:oak_log"], "search_radius": 16})

    assert result.success is True
    assert result.reason == "collected"
    assert result.metrics["inventory_delta"]["after"] == 1
    assert server.verdict_seen == [("mp-1", True)]


def test_collect_block_denied_stays_a_typed_failure() -> None:
    policy = GovernancePolicy(
        natural_regions=[Region("n", (-64, 0, -64), (64, 200, 64))],
        protected_regions=[Region("base", (0, 0, 0), (10, 128, 10))],
    )
    client = _client(FakeBodyServer(), GovernanceAnswerer(policy))
    client.connect()
    registry = _registry(client)

    result = registry.get("collect_block").callable({"block_types": ["minecraft:oak_log"]})

    assert result.success is False
    assert result.reason == "candidate_targets_exhausted"


def test_collect_block_requires_block_types() -> None:
    client = _client(FakeBodyServer())
    client.connect()
    registry = _registry(client)

    result = registry.get("collect_block").callable({"block_types": []})

    assert result.success is False
    assert result.reason == "no_block_types"


def test_projectors_bound_the_model_visible_summary() -> None:
    registry = _registry(_client(FakeBodyServer()))

    nav = registry.get("navigate_to").projector("arrived", {"replans": 2, "final_x": 10.0, "expanded_nodes": 913})
    assert nav == {"reason": "arrived", "replans": 2, "expanded_nodes": 913}
    assert "final_x" not in nav

    col = registry.get("collect_block").projector(
        "collected", {"inventory_delta": {"item_id": "minecraft:oak_log", "after": 1}, "candidates_tried": 1}
    )
    assert col["inventory_delta"]["after"] == 1
    assert col["candidates_tried"] == 1
