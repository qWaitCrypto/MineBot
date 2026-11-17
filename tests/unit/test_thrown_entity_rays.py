from types import SimpleNamespace
from unittest.mock import patch

from minebot.body.thrown_entity_rays import intersect_thrown_entity_rays
from minebot.body import use as use_module
from minebot.body.use import UseTransactions
from minebot.contract import ToolResult
from minebot.contract import PerceptionResult


def test_two_forward_rays_report_crossing_as_a_fact():
    result = intersect_thrown_entity_rays(
        {"throw_position": [0, 64, 0], "direction": [1, 0]},
        {"throw_position": [10, 64, 10], "direction": [0, -1]},
    )
    assert result == {
        "status": "intersection",
        "position": [10.0, 0.0],
        "distance_from_second": 10.0,
        "fact": "The two throws cross at x=10.0, z=0.0, about 10 blocks from here.",
    }


def test_parallel_and_behind_rays_have_no_crossing_fact():
    assert intersect_thrown_entity_rays(
        {"throw_position": [0, 64, 0], "direction": [1, 0]},
        {"throw_position": [0, 64, 1], "direction": [1, 1e-8]},
    ) == {"status": "near_parallel"}
    assert intersect_thrown_entity_rays(
        {"throw_position": [0, 64, 0], "direction": [1, 0]},
        {"throw_position": [-10, 64, 10], "direction": [0, -1]},
    ) == {"status": "behind"}


def test_use_item_reports_generic_snowball_path_without_eye_specific_handling():
    # Removing metrics["thrown_entity"] = thrown in UseTransactions.use_item fails below.
    snapshots = iter([
        {"game_time": 0, "entities": []},
        {"game_time": 1, "entities": [{"type": "snowball", "id": "s1", "pos": [0, 65, 0]}]},
        {"game_time": 4, "entities": [{"type": "snowball", "id": "s1", "pos": [3, 65, 0]}]},
        {"game_time": 5, "entities": []},
    ])
    body = SimpleNamespace(get_state=lambda: SimpleNamespace(pos=(0.0, 64.0, 0.0), effects=[]))
    runtime = UseTransactions(body)
    empty_inventory = PerceptionResult(bot="Bot1", scope="inventory", type="perception", ok=True,
        complete=True, data={"slots": []})
    with patch.object(use_module, "_nearby_entity_facts", side_effect=lambda _: next(snapshots)), \
         patch.object(use_module, "_read_inventory", return_value=empty_inventory), \
         patch.object(use_module, "_dispatch", return_value=ToolResult(
             success=True, reason="completed", can_retry=False, metrics={"stopped_reason": "completed"})), \
         patch.object(use_module, "_effect_delta_metrics", return_value={"effect_delta": 0}), \
         patch.object(use_module, "_watched_item_metrics", return_value={"watched_item_deltas": {}}):
        result = runtime.use_item(item=None)

    assert result.reason == "use_unverified"
    thrown = result.metrics["thrown_entity"]
    assert thrown["entity_type"] == "snowball"
    assert thrown["first_position"] == [0.0, 65.0, 0.0]
    assert thrown["last_position"] == [3.0, 65.0, 0.0]
    assert thrown["direction"] == [1.0, 0.0]
    assert thrown["yaw_degrees"] == -90.0
    assert thrown["ticks_observed"] == 3
    assert thrown["ended"] == "gone"
    assert "thrown_entity_crossing" not in result.metrics


def test_second_same_type_use_item_result_includes_crossing_fact():
    # Removing metrics["thrown_entity"] = thrown in UseTransactions.use_item fails below.
    snapshots = iter([
        {"game_time": 0, "entities": []},
        {"game_time": 1, "entities": [{"type": "eye_of_ender", "id": "e1", "pos": [0, 65, 0]}]},
        {"game_time": 4, "entities": [{"type": "eye_of_ender", "id": "e1", "pos": [3, 65, 0]}]},
        {"game_time": 5, "entities": []},
        {"game_time": 10, "entities": []},
        {"game_time": 11, "entities": [{"type": "eye_of_ender", "id": "e2", "pos": [10, 65, 10]}]},
        {"game_time": 14, "entities": [{"type": "eye_of_ender", "id": "e2", "pos": [10, 65, 7]}]},
        {"game_time": 15, "entities": []},
    ])
    positions = iter([(0.0, 64.0, 0.0), (0.0, 64.0, 0.0),
                      (10.0, 64.0, 10.0), (10.0, 64.0, 10.0)])
    body = SimpleNamespace(get_state=lambda: SimpleNamespace(pos=next(positions), effects=[]))
    runtime = UseTransactions(body)
    inventory_reads = iter([
        PerceptionResult(bot="Bot1", scope="inventory", type="perception", ok=True, complete=True,
            data={"slots": [{"slot": 0, "item": "ender_eye", "count": count, "empty": count == 0}]})
        for count in (1, 0, 1, 0)
    ])
    with patch.object(use_module, "_nearby_entity_facts", side_effect=lambda _: next(snapshots)), \
         patch.object(use_module, "_read_inventory", side_effect=lambda _: next(inventory_reads)), \
         patch.object(use_module, "_dispatch", return_value=ToolResult(
             success=True, reason="completed", can_retry=False, metrics={"stopped_reason": "completed"})), \
         patch.object(use_module, "_effect_delta_metrics", return_value={"effect_delta": 0}), \
         patch.object(use_module, "_watched_item_metrics", return_value={"watched_item_deltas": {}}):
        runtime.inventory.equip_item = lambda **_: ToolResult(success=True, reason="equipped", can_retry=False)
        first = runtime.use_item(item="ender_eye")
        second = runtime.use_item(item="ender_eye")

    assert first.success and second.success
    assert second.metrics["thrown_entity_crossing"] == {
        "status": "intersection",
        "position": [10.0, 0.0],
        "distance_from_second": 10.0,
        "fact": "The two throws cross at x=10.0, z=0.0, about 10 blocks from here.",
    }
    assert second.metrics["thrown_entity"]["fact"].endswith(
        "The two throws cross at x=10.0, z=0.0, about 10 blocks from here.")
