"""Search intentions, moving observations and actual cancel settlement."""
import math
import threading
from contextlib import closing

import pytest
from jsonschema import validate

from minebot.app.exploration import PersistentExplorationCoverageStore
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.contract import ExecutionCancellation, ExecutionCancelled, Region, execution_cancellation_scope
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientTerminal
from tests.unit.test_real_client_body import FakeClientIpc


class WalkingIpc(FakeClientIpc):
    def __init__(self, block_x=None):
        super().__init__()
        self.pos = [0, 70, 0]
        self.block_x = block_x
        self.moving = threading.Event()
        self.navigation_reads = 0

    def state(self):
        return {**super().state(), "position": list(self.pos), "entities": []}

    def command(self, objective, *, action_id, timeout_ms):
        self.commands.append((objective, action_id, timeout_ms))
        self._cancel.clear()
        parts = objective.split(":")
        goal = [int(parts[1]), int(parts[2]), int(parts[3])] if parts[0] == "near" else [int(parts[1]), 70, int(parts[2])]
        self.moving.set()
        while math.dist(self.pos, goal) > 0.1 and not self._cancel.wait(0.03):
            length = math.dist(self.pos, goal)
            self.pos = [a+(b-a)*min(1, 1/length) for a, b in zip(self.pos, goal)]
        self.moving.clear()
        canceled = self._cancel.is_set()
        return RealClientTerminal(action_id, "canceled" if canceled else "completed",
                                  "cancel_requested" if canceled else "goal_reached", {"position": self.pos})

    def world_read(self, scope, params):
        if self.moving.is_set():
            self.navigation_reads += 1
        if scope == "environment":
            columns = params.get("columns", [[-16, 0], [16, 0]])
            return {"ok": True, "complete": True, "data": {
                "pos": list(self.pos), "dimension": "minecraft:overworld", "space": "surface", "difficulty": "peaceful",
                "samples": [{"xz": [x,z], "loaded": True, "surface_pos": [x,70,z], "standable": True,
                             "biome": "minecraft:plains", "surface_block": "minecraft:grass_block"} for x,z in columns]}}
        if scope == "findBlocks":
            found = self.block_x is not None and abs(self.pos[0]-self.block_x) <= params["radius"]
            return {"ok": True, "complete": True, "data": {"blocks": [
                {"x": self.block_x, "y": 70, "z": 0, "type": "minecraft:iron_ore"}] if found else []}}
        return super().world_read(scope, params)


def setup(ipc=None, coverage=None):
    ipc = ipc or WalkingIpc()
    body = RealClientBody("Explorer", ipc)
    registry = build_phase1_registry(body, Phase1RuntimeConfig(Region("test", (-100,0,-100), (500,300,500)),
                                    body_provider="real_client", exploration_coverage_store=coverage))
    return ipc, body, registry


@pytest.mark.parametrize("selection,expected", [({"destination": [48,0]}, [48,70,0]),
    ({"direction": "north", "max_distance": 48}, [0,70,-48]),
    ({"region": {"min": [12,0], "max": [24,12]}}, [12,70,12])])
def test_model_selected_route_and_environment(selection, expected):
    ipc, body, registry = setup()
    tool = registry.get("explore_for")
    validate(selection, tool.input_schema)
    result = tool.callable(selection)
    assert result.success and result.reason == "route_complete"
    assert math.dist(result.metrics["final_pos"], expected) < 2
    assert not result.metrics["target_found"]
    projection = tool.projector(result.reason, result.metrics)
    assert all("action_id" not in leg for leg in projection["navigation_legs"])
    assert body.get_state().pending_action_count == 0
    environment = registry.get("observe_environment").callable({})
    assert environment.metrics["difficulty"] == "peaceful"
    assert environment.metrics["recent_searches"]


def test_target_found_during_same_navigation_stops_before_destination():
    ipc, body, registry = setup(WalkingIpc(block_x=40))
    result = registry.get("explore_for").callable({"destination": [160,0], "block_targets": ["iron_ore"], "scan_radius": 4})
    assert result.success and result.reason == "found"
    assert result.metrics["blocks"][0]["pos"] == [40,70,0]
    assert 24 < result.metrics["final_pos"][0] < 48
    assert ipc.navigation_reads > 0 and ipc.canceled
    assert body.get_state().pending_action_count == 0


def test_cancel_settles_active_search_without_next_leg():
    ipc, body, registry = setup()
    token = ExecutionCancellation()
    timer = threading.Thread(target=lambda: (ipc.moving.wait(2), token.cancel("user_cancel")))
    timer.start()
    with execution_cancellation_scope(token), pytest.raises(ExecutionCancelled):
        registry.get("explore_for").callable({"direction": "east", "max_distance": 1000})
    timer.join()
    assert len(ipc.commands) == 1 and ipc.canceled
    assert body.get_state().pending_action_count == 0


def test_history_survives_changed_targets_and_store_reopen(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    scope = RuntimeScope("server", "world", "Explorer")
    with closing(RuntimeStateStore(path)) as store:
        coverage = PersistentExplorationCoverageStore(store, scope)
        ipc, body, registry = setup(coverage=coverage)
        result = registry.get("explore_for").callable({"destination": [24,0], "entity_targets": ["sheep"]})
        assert result.reason == "route_complete" and not result.metrics["target_found"]
        registry.get("explore_for").callable({"destination": [48,0], "entity_targets": ["sheep", "spider"]})
    with closing(RuntimeStateStore(path)) as store:
        coverage = PersistentExplorationCoverageStore(store, scope)
        _, _, registry = setup(coverage=coverage)
        environment = registry.get("observe_environment").callable({})
        assert environment.metrics["visited_region_count"] >= 3
        targets = [record["targets"]["entities"] for record in environment.metrics["recent_searches"]]
        assert ["sheep"] in targets and ["sheep", "spider"] in targets


def test_selection_required_and_no_underground_guessing():
    ipc, _, registry = setup()
    tool = registry.get("explore_for")
    assert tool.callable({"entity_targets": ["sheep"]}).reason == "choose_one_exploration_destination"
    assert not tool.callable({"direction": "east", "space": "underground"}).success
    assert ipc.commands == []


def test_partial_region_resumes_ordered_survey_without_a_cursor():
    ipc, _, registry = setup()
    tool = registry.get("explore_for")
    selection = {"region": {"min": [0,0], "max": [24,24]}, "return_policy": "region_budget"}
    first = tool.callable({**selection, "max_distance": 16})
    assert first.reason == "search_budget_exhausted"
    before = len(ipc.commands)
    second = tool.callable(selection)
    assert second.reason == "route_complete" and second.metrics["survey_origin"] == [0,70,0]
    assert ipc.commands[before][0] != ipc.commands[0][0]
    before = len(ipc.commands)
    assert tool.callable(selection).reason == "route_complete"
    assert len(ipc.commands) == before
