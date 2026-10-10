"""Admission and progress truth for native (Baritone-owned) Body processes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from minebot.app.client_body_tools import client_body_tools
from minebot.body import cluster_collection
from minebot.body.cluster_collection import collect_native_cluster
from minebot.body.inventory import _parse_recipe_variants
from minebot.body.native_admission import harvest_tool_denial, required_tier_for, work_volume_denial
from minebot.body.resource_collection import ResourceCollectionConfig
from minebot.brain.acquisition import AcquisitionError, resolve_acquisition
from minebot.contract import Event, PerceptionResult, Region, Result
from minebot.contract.results import RECIPE_SOURCE_UNLOCKED_BOOK
from minebot.game.errors import BodyActionTimeoutError

REGION = Region("work", (-64, 0, -64), (64, 200, 64))


class NativeBody:
    """A disposable-world client whose mining action yields what the test says."""

    bot_name = "Bot1"

    def __init__(self, inventory: dict[str, int], *, gain_per_action: int = 0, finishes: bool = True) -> None:
        self.inventory = dict(inventory)
        self.gain_per_action = gain_per_action
        self.finishes = finishes
        self.actions: list = []
        self.interrupted: list[str] = []
        self.pos = (0.5, 64.0, 0.5)

    def get_state(self):
        return SimpleNamespace(inventory_counts=dict(self.inventory), pos=self.pos, selected_item="oak_log")

    def perceive(self, scope, params):
        data = {"disposable_world": True} if scope == "baritoneState" else {"blocks": []}
        return PerceptionResult(bot=self.bot_name, scope=scope, type="perception", ok=True, complete=True, data=data)

    def execute(self, action):
        self.actions.append(action)
        return Result(id=action.id, bot=self.bot_name, type="result", ok=True, accepted=True, complete=False)

    def await_action_terminal(self, action_id, timeout_s=15.0, **_):
        if self.interrupted:
            return Event(seq=2, tick=0, bot=self.bot_name, name="cancelled", data={"stopped_reason": "cancelled"})
        if not self.finishes:
            raise BodyActionTimeoutError("still mining", diagnostics={})
        self.inventory["cobblestone"] = self.inventory.get("cobblestone", 0) + self.gain_per_action
        return Event(seq=1, tick=0, bot=self.bot_name, name="completed", data={"completed": True})

    def interrupt(self, reason=None):
        self.interrupted.append(str(reason))


def _collect(body, count=8):
    return collect_native_cluster(
        body, block_types=("stone",), expected_drops=("cobblestone",), remaining_count=count, dry=False,
        config=ResourceCollectionConfig(max_wall_s=30.0, mutation_budget=4),
    )


def test_required_tier_is_the_highest_any_block_needs() -> None:
    assert required_tier_for(["oak_log"]) is None
    assert required_tier_for(["stone", "minecraft:iron_ore"]) == "stone"


def test_harvest_tool_denial_names_the_missing_tier() -> None:
    bare = NativeBody({"oak_log": 5})
    wooden = NativeBody({"wooden_pickaxe": 1})

    denied = harvest_tool_denial(bare, ["stone"])
    assert denied.reason == "missing_required_tool"
    assert denied.metrics["required_tier"] == "wooden" and denied.metrics["best_owned"] is None
    assert harvest_tool_denial(bare, ["oak_log"]) is None
    assert harvest_tool_denial(wooden, ["stone"]) is None
    assert harvest_tool_denial(wooden, ["iron_ore"]).metrics["best_owned"] == {"item": "wooden_pickaxe", "tier": "wooden"}


def test_native_collection_refuses_to_mine_stone_without_a_pickaxe() -> None:
    body = NativeBody({"oak_log": 5})

    result = _collect(body)

    assert result.reason == "missing_required_tool"
    assert body.actions == []  # nothing was sent to the client


def test_native_collection_notices_a_pickaxe_lost_between_actions() -> None:
    body = NativeBody({"wooden_pickaxe": 1}, gain_per_action=3)
    original = body.await_action_terminal

    def break_pickaxe_after_first(action_id, **kwargs):
        event = original(action_id, **kwargs)
        body.inventory.pop("wooden_pickaxe", None)
        return event

    body.await_action_terminal = break_pickaxe_after_first

    result = _collect(body, count=20)

    assert result.reason == "missing_required_tool"
    assert len(body.actions) == 1
    assert result.metrics["collected"] == 3


def test_native_collection_stops_a_process_that_gains_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_collection, "NO_PROGRESS_WINDOW_S", 0.05)
    monkeypatch.setattr(cluster_collection, "_PROGRESS_POLL_S", 0.01)
    body = NativeBody({"wooden_pickaxe": 1}, finishes=False)

    result = _collect(body)

    assert result.reason == "no_progress"
    assert body.interrupted == ["no_progress"]
    assert result.metrics["collected"] == 0
    assert result.metrics["attempts"][0]["no_progress"]["selected_item"] == "oak_log"


def test_native_collection_stops_when_the_path_keeps_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_collection, "_PROGRESS_POLL_S", 0.01)
    body = NativeBody({"wooden_pickaxe": 1}, finishes=False)
    failed = iter(range(0, 1000, 2))
    plain = body.perceive

    def perceive(scope, params):
        read = plain(scope, params)
        if scope == "baritoneState":
            read.data.update(path_events={"CALC_FAILED": next(failed)}, baritone_goal="GoalBreak{x=1,y=76,z=1}",
                             navigation={"last_failure": "movement_unreachable"}, scaffold_blocks=0)
        return read

    body.perceive = perceive

    result = _collect(body)

    assert result.reason == "path_unreachable"
    assert body.interrupted == ["path_unreachable"]
    stalled = result.metrics["stalled"]
    assert stalled["failed_path_calculations"] >= 5 and "y=76" in stalled["goal"]
    assert stalled["target"] == [1, 76, 1] and stalled["target_above_feet"] == 12
    assert stalled["native_failure"] == "movement_unreachable"
    assert stalled["scaffold_blocks"] == 0
    assert "12 blocks above" in result.next_suggestion and "carries 0 blocks" in result.next_suggestion
    assert "retry_unreachable" in result.next_suggestion


def test_native_collection_still_completes_when_it_produces() -> None:
    body = NativeBody({"wooden_pickaxe": 1}, gain_per_action=8)

    result = _collect(body)

    assert result.success and result.reason == "resource_collected"


def test_work_volume_must_lie_inside_a_natural_region() -> None:
    assert work_volume_denial([REGION], (0, 64, 0), (9, 70, 9)) is None
    assert work_volume_denial([], (0, 64, 0), (900, 70, 9)) is None  # no region governance configured
    denied = work_volume_denial([REGION], (60, 64, 0), (70, 70, 9))
    assert denied.reason == "outside_natural_region"


def test_native_build_outside_the_region_is_refused_before_reaching_the_client() -> None:
    body = Mock()
    tools = {tool.name: tool for tool in client_body_tools(body, [REGION])}

    result = tools["build_structure"].callable({"anchor": [60, 64, 0], "size": [10, 1, 10], "block": "cobblestone"})

    assert result.reason == "outside_natural_region"
    body.execute.assert_not_called()
    assert tools["clear_area"].callable({"start": [0, 64, 0], "end": [500, 64, 0]}).reason == "outside_natural_region"
    assert tools["farm_area"].callable({"anchor": [60, 64, 0], "range": 16}).reason == "outside_natural_region"


def _recipe_answer(source: str | None) -> PerceptionResult:
    data = {"variants": []}
    if source:
        data["recipe_source"] = source
    return PerceptionResult(bot="Bot1", scope="recipeData", type="perception", ok=True, complete=True, data=data)


def test_empty_recipe_book_answer_is_not_unlocked_rather_than_not_found() -> None:
    locked = _parse_recipe_variants("stone_pickaxe", _recipe_answer(RECIPE_SOURCE_UNLOCKED_BOOK))
    absent = _parse_recipe_variants("stone_pickaxe", _recipe_answer(None))

    assert locked.reason == "recipe_not_unlocked" and locked.can_retry
    assert "wiki" in locked.next_suggestion
    assert absent.reason == "recipe_not_found" and not absent.can_retry


def test_planner_reports_that_the_missing_recipe_is_only_locked() -> None:
    def lookup(item):
        return []

    lookup.recipe_source = RECIPE_SOURCE_UNLOCKED_BOOK

    plan = resolve_acquisition("stone_pickaxe", 1, {}, lookup, max_depth=18)

    assert isinstance(plan, AcquisitionError)
    assert plan.reason == "unplannable"
    assert plan.detail == {"missing_recipe": "stone_pickaxe", "recipe_source": RECIPE_SOURCE_UNLOCKED_BOOK}


def test_unreachable_target_is_remembered_and_can_be_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_collection, "_PROGRESS_POLL_S", 0.01)
    body = NativeBody({"wooden_pickaxe": 1}, finishes=False)
    failed = iter(range(0, 1000, 2))
    plain = body.perceive

    def perceive(scope, params):
        read = plain(scope, params)
        if scope == "baritoneState":
            read.data.update(path_events={"CALC_FAILED": next(failed)}, baritone_goal="GoalBreak{x=1,y=76,z=1}")
        return read

    body.perceive = perceive
    memory: set = set()
    config = ResourceCollectionConfig(max_wall_s=30.0, mutation_budget=4)

    def collect(**overrides):
        return collect_native_cluster(
            body, block_types=("stone",), expected_drops=("cobblestone",), remaining_count=8, dry=False,
            config=ResourceCollectionConfig(max_wall_s=30.0, mutation_budget=4, **overrides), unreachable=memory)

    first = collect()
    assert first.reason == "path_unreachable" and memory == {(1, 76, 1)}
    assert first.metrics["avoided_targets"] == [[1, 76, 1]]

    body.interrupted.clear()
    retried = collect(retry_unreachable=True)
    assert retried.reason == "path_unreachable" and memory == {(1, 76, 1)}  # tried again, failed again
    assert config.avoid == ()


def test_avoided_log_is_never_selected() -> None:
    body = NativeBody({})
    plain = body.perceive

    def perceive(scope, params):
        read = plain(scope, params)
        if scope == "findBlocks":
            read.data["blocks"] = [{"x": 137, "y": 76, "z": 109}]
        return read

    body.perceive = perceive
    result = collect_native_cluster(
        body, block_types=("spruce_log",), expected_drops=("spruce_log",), remaining_count=8, dry=False,
        config=ResourceCollectionConfig(max_wall_s=30.0, mutation_budget=4, avoid=((137, 76, 109),)))

    assert result.reason == "resource_candidates_not_found" and body.actions == []
    assert result.metrics["avoided_targets"] == [[137, 76, 109]]


def test_a_pickaxe_that_breaks_mid_action_stops_it_at_once_and_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_collection, "_PROGRESS_POLL_S", 0.01)  # the 90 s stall window is left as it is
    body = NativeBody({"wooden_pickaxe": 1, "cobblestone": 0}, finishes=False)
    waiting = body.await_action_terminal
    polls = []

    def mine_then_break(action_id, **kwargs):
        polls.append(action_id)
        if len(polls) == 2 and not body.interrupted:
            body.inventory["cobblestone"] = 57
            body.inventory.pop("wooden_pickaxe")
        return waiting(action_id, **kwargs)

    body.await_action_terminal = mine_then_break

    result = _collect(body, count=64)

    assert body.interrupted == ["tool_lost"]
    assert result.reason == "missing_required_tool" and result.can_retry is False
    assert "pickaxe" in result.next_suggestion
    assert result.metrics["collected"] == 57 and result.metrics["remaining_count"] == 7


def _tree_body(outcomes):
    """Three single-log trees; each collect action ends as the next outcome says."""
    body = NativeBody({"oak_log": 0})
    logs = [(10, 64, 0), (20, 64, 0), (30, 64, 0)]
    plain = body.perceive

    def perceive(scope, params):
        read = plain(scope, params)
        if scope == "findBlocks":
            read.data["blocks"] = [{"x": x, "y": y, "z": z} for x, y, z in logs]
        return read

    def terminal(action_id, **_):
        reason, gain = outcomes.pop(0)
        body.inventory["oak_log"] += gain
        done = reason == "resource_cluster_collected"
        return Event(seq=1, tick=0, bot=body.bot_name, name="completed" if done else "failed",
                     data={"completed": done, "stopped_reason": reason})

    body.perceive, body.await_action_terminal = perceive, terminal
    return body


def _collect_logs(body, count):
    return collect_native_cluster(
        body, block_types=("oak_log",), expected_drops=("oak_log",), remaining_count=count, dry=False,
        config=ResourceCollectionConfig(max_wall_s=30.0, mutation_budget=8))


def test_one_cluster_whose_drops_were_not_all_picked_up_does_not_end_the_collection(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_collection, "resource_cluster", lambda body, anchor, *args, **kwargs: [anchor])
    body = _tree_body([("resource_cluster_collected", 5), ("cluster_pickup_not_observed", 4),
                       ("resource_cluster_collected", 6)])

    result = _collect_logs(body, 15)

    assert result.success and result.reason == "resource_collected"
    assert len(body.actions) == 3 and result.metrics["collected"] == 15
    (unfinished,) = result.metrics["unfinished_clusters"]
    assert (unfinished["reason"], unfinished["cells"], unfinished["gained"]) == ("cluster_pickup_not_observed", 1, 4)
    # The cluster that was left is not chosen again.
    assert tuple(unfinished["target"]) not in {tuple(action.params["targets"][0]) for action in body.actions[2:]}


def test_clusters_that_keep_yielding_nothing_still_end_the_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_collection, "resource_cluster", lambda body, anchor, *args, **kwargs: [anchor])
    body = _tree_body([("cluster_pickup_not_observed", 0), ("cluster_pickup_not_observed", 0),
                       ("resource_cluster_collected", 6)])

    result = _collect_logs(body, 15)

    assert not result.success and result.reason == "cluster_pickup_not_observed"
    assert len(body.actions) == 2 and result.metrics["unfinished_cluster_count"] == 1
