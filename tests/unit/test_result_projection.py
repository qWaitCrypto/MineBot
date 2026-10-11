"""What the model reads of a result: the answer and the outcome, not the client's state frame."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from minebot.app.body_capability_tools import (
    nearby_blocks_observation_projector,
    recipe_observation_projector,
)
from minebot.app.context_compaction import MAX_ITEM_CHARS, bound_item
from minebot.app.reflex_tools import ReflexActivity, ReflexManager, reflex_observation_projector
from minebot.app.projection import bounded_summary_value
from minebot.app.runner import BackgroundCall, _model_tool_payload
from minebot.app.work_runtime import WorkRunResult
from minebot.game import real_client_frame
from minebot.game.real_client_frame import (
    learn_client_state_fields,
    reset_client_state_fields,
    without_ambient_state,
)


def _state_frame() -> dict:
    return {
        "type": "state", "request_id": "r1", "protocol": "minebot-client-spike/1",
        "position": [1.5, 64.0, 2.5], "health": 20.0, "food": 20, "pid": 4242,
        "heap_used_bytes": 369_663_552, "random_looking": 0.01,
        "inventory": {"oak_log": 5},
        "inventory_slots": [{"slot": index, "empty": True, "count": 0} for index in range(46)],
        "entities": [{"name": "Cow", "pos": [index, 64, 0], "dist2": 9.0 * index} for index in range(40)],
        "builder_placeable": ["minecraft:air"] * 36,
        "navigation": {"calculating": False, "dest": [3, 64, 4]},
    }


def _terminal_frame(**action) -> dict:
    frame = {key: value for key, value in _state_frame().items() if key != "request_id"}
    frame.update({"type": "terminal", "action_id": "a-1", "status": "completed", "reason": "goal_reached",
                  "objective": "near:3:64:4:1", "elapsed_ms": 1200, "inventory_delta": 0,
                  "path_events": {"CALC_STARTED": 1}})
    frame.update(action)
    return frame


@pytest.fixture(autouse=True)
def _known_state_frame():
    reset_client_state_fields()
    learn_client_state_fields(_state_frame())
    yield
    reset_client_state_fields()


def test_state_fields_are_learned_from_the_client_reply_not_listed():
    fields = real_client_frame.client_state_fields()
    assert {"entities", "inventory_slots", "heap_used_bytes", "random_looking"} <= fields
    assert "type" not in fields and "request_id" not in fields
    learn_client_state_fields({"type": "terminal", "anything": 1})
    assert "anything" not in real_client_frame.client_state_fields()


def test_action_reply_keeps_what_the_action_did_and_where_it_ended():
    shown = without_ambient_state(_terminal_frame())
    assert shown["reason"] == "goal_reached" and shown["elapsed_ms"] == 1200
    assert shown["path_events"] == {"CALC_STARTED": 1}
    assert shown["position"] == [1.5, 64.0, 2.5]
    for ambient in ("entities", "inventory_slots", "builder_placeable", "heap_used_bytes", "pid", "inventory"):
        assert ambient not in shown
    assert "navigation" not in shown


def test_failed_action_keeps_where_the_path_stopped():
    shown = without_ambient_state(_terminal_frame(status="failed", reason="baritone_stopped_without_result"))
    assert shown["navigation"] == {"calculating": False, "dest": [3, 64, 4]}


def test_state_reply_and_plain_results_are_left_whole():
    state = _state_frame()
    assert without_ambient_state(state) == state
    plain = {"pos": [1, 2, 3], "health": 20.0, "inventory": {"oak_log": 5}}
    assert without_ambient_state(plain) == plain


def test_every_step_of_a_composite_result_loses_its_copy_of_the_frame():
    raw = {"crafted_count": 1, "workspace": {"place": {"metrics": _terminal_frame(reason="placed")}},
           "attempts": [{"mine": {"metrics": _terminal_frame(inventory_delta=5)}} for _ in range(5)]}
    shown = without_ambient_state(raw)
    assert len(json.dumps(shown)) < len(json.dumps(raw)) / 10
    assert shown["workspace"]["place"]["metrics"]["reason"] == "placed"
    assert [step["mine"]["metrics"]["inventory_delta"] for step in shown["attempts"]] == [5] * 5
    assert "entities" in raw["attempts"][0]["mine"]["metrics"]  # the stored result is untouched


def test_nothing_is_removed_before_the_client_has_described_its_frame():
    reset_client_state_fields()
    frame = _terminal_frame()
    assert without_ambient_state(frame) is frame


def test_model_payload_of_an_action_omits_the_frame_and_does_not_list_it_as_omitted():
    result = {"success": True, "reason": "arrived", "canRetry": False, "metrics": _terminal_frame()}
    payload = _model_tool_payload("move_to", result, trace_ref="call-1")
    summary = payload["summary"]
    assert summary["objective"] == "near:3:64:4:1" and summary["position"] == [1.5, 64.0, 2.5]
    for ambient in ("heap_used_bytes", "random_looking", "pid", "entities", "entity_count"):
        assert ambient not in summary
    assert payload["projection"]["omittedFields"] == []
    assert len(json.dumps(payload)) < 900


def test_state_read_shows_a_whole_inventory():
    counts = {f"item_{index}": index + 1 for index in range(30)}
    result = {"success": True, "reason": "state_read", "metrics": {"pos": [1, 64, 2], "inventory_counts": counts}}
    summary = _model_tool_payload("read_state", result, trace_ref="call-2")["summary"]
    assert summary["inventory_counts"] == counts
    assert summary["inventory_counts_counts_complete"] is True


def test_nearby_blocks_are_all_shown_grouped_by_kind():
    blocks = [{"type": "minecraft:grass_block", "state": "SOLID", "properties": {"snowy": "false"},
               "x": x, "y": 63, "z": z} for x in range(10) for z in range(10)]
    blocks += [{"type": "minecraft:oak_log", "state": "SOLID", "properties": {"axis": "y"},
                "x": 3, "y": 64 + dy, "z": 3} for dy in range(5)]
    metrics = {"complete": True, "data": {"blocks": blocks, "total": len(blocks)}}
    summary = nearby_blocks_observation_projector("nearby_blocks_read", metrics)
    assert summary["blocks_complete"] is True and summary["blocks_shown"] == 105
    assert sum(len(group["positions"]) for group in summary["blocks"]) == 105
    assert len(summary["blocks"]) == 2
    logs = next(group for group in summary["blocks"] if group["type"] == "minecraft:oak_log")
    assert logs["properties"] == {"axis": "y"} and [3, 68, 3] in logs["positions"]
    assert len(json.dumps(summary)) < len(json.dumps(blocks)) / 4


def test_an_unusually_varied_neighbourhood_says_how_much_it_shows():
    blocks = [{"type": f"minecraft:kind_{index}", "state": "SOLID", "properties": {"note": "x" * 120},
               "x": index, "y": 64, "z": 0} for index in range(128)]
    summary = nearby_blocks_observation_projector("nearby_blocks_read", {"complete": True, "data": {"blocks": blocks}})
    assert summary["block_count"] == 128 and 8 <= summary["blocks_shown"] < 128
    assert summary["blocks_complete"] is False


def test_recipe_read_shows_the_recipe():
    data = {"item": "stone_pickaxe", "recipes": [{"ingredient_groups": [["cobblestone"] * 3, ["stick"] * 2],
                                                 "station": "crafting_table"}] * 4}
    result = {"success": True, "reason": "recipe_read", "metrics": {"scope": "recipeData", "complete": True, "data": data}}
    payload = _model_tool_payload("read_recipe", result, trace_ref="call-3", projector=recipe_observation_projector)
    assert payload["summary"]["data"] == data and payload["summary"]["data_complete"] is True
    assert "metrics.data" not in payload["projection"]["omittedFields"]


def test_one_oversized_item_is_cut_the_same_way_every_time():
    item = {"role": "user", "content": "REFLEX_RESULT: " + "x" * (MAX_ITEM_CHARS * 3)}
    first, removed = bound_item(item)
    second, _ = bound_item(item)
    assert removed == len(item["content"]) - MAX_ITEM_CHARS
    assert first == second and len(first["content"]) < MAX_ITEM_CHARS + 300
    assert "characters omitted" in first["content"]
    small = {"type": "function_call_output", "call_id": "c", "output": "ok"}
    assert bound_item(small) == (small, 0)


def _manager_with_finished_plan(raw_results: dict[str, dict]) -> tuple[ReflexManager, ReflexActivity, list]:
    stored: list = []

    def shown_plan_result(tool_name, result, trace_ref):
        stored.append((tool_name, trace_ref))
        payload = _model_tool_payload(tool_name, result, trace_ref=trace_ref,
                                      observation_handle=f"observation:{trace_ref}")
        return payload

    manager = ReflexManager.__new__(ReflexManager)
    manager.runtime = SimpleNamespace(shown_plan_result=shown_plan_result)
    steps = [{"kind": "call", "id": node_id, "tool": "craft_item", "arguments": {"item": node_id},
              "_runtime": {"status": "done", "updated_at": 1.0, "result": result}}
             for node_id, result in raw_results.items()]
    tree = {"language": "minebot-work/0.2", "goal": "tools", "timeout_s": 60,
            "work": {"kind": "sequence", "intent": "craft", "steps": steps, "_runtime": {"status": "done"}}}
    activity = ReflexActivity("activity-1", {"work": {}}, status="completed",
                              result=WorkRunResult("completed", tree, {}, {}))
    return manager, activity, stored


def test_plan_tree_shows_each_call_like_a_direct_call_and_keeps_the_original():
    raw = {"success": True, "reason": "completed", "canRetry": False,
           "metrics": {"crafted_count": 1, "item": "wooden_pickaxe",
                       "workspace": {"place": {"metrics": _terminal_frame()}},
                       "craft_attempts": [{"metrics": {"craft": _terminal_frame()}}]}}
    manager, activity, stored = _manager_with_finished_plan({"table": raw, "pickaxe": raw})
    shown = manager._shown_tree(activity)
    assert len(json.dumps(shown)) < 4_000 < len(json.dumps(activity.result.tree))
    first = shown["work"]["steps"][0]["_runtime"]["result"]
    assert first["success"] is True and first["summary"]["crafted_count"] == 1
    assert first["observationHandle"] == "observation:plan-table"
    assert "entities" in json.dumps(activity.result.tree)  # executor's tree still has the originals
    manager._shown_tree(activity)
    assert stored == [("craft_item", "plan-table"), ("craft_item", "plan-pickaxe")]  # projected once


def test_blocked_node_does_not_repeat_its_result():
    raw = {"success": False, "reason": "unplannable", "canRetry": False, "metrics": {"item": "stone_pickaxe"}}
    manager, activity, _ = _manager_with_finished_plan({"tool": raw})
    node = activity.result.tree["work"]["steps"][0]["_runtime"]
    node.update(status="blocked", blocked={"reason": "unplannable", "result": raw})
    shown = manager._shown_tree(activity)["work"]["steps"][0]["_runtime"]
    assert shown["blocked"] == {"reason": "unplannable"}
    assert shown["result"]["reason"] == "unplannable"


def test_read_reflex_shows_the_tree_it_was_asked_for():
    tree = {"goal": "tools", "work": {"kind": "call", "tool": "craft_item", "_runtime": {"status": "done"}}}
    assert reflex_observation_projector("ok", {"tree": tree}) == {"tree": tree, "tree_complete": True}


def test_long_text_is_bounded_wherever_it_sits_and_stays_behind_the_handle():
    assert len(json.dumps(bounded_summary_value({"outer": {"text": "x" * 50_000}}))) < 400
    result = {"success": True, "reason": "skill_created", "metrics": {"name": "n", "body": "y" * 900}}
    summary = _model_tool_payload("create_skill", result, trace_ref="call-4")["summary"]
    assert summary == {"name": "n"}


def test_background_receipt_does_not_read_as_a_finished_call():
    call = BackgroundCall("call-5", "collect_resource", {"item": "oak_log"}, task=None)
    payload = _model_tool_payload("collect_resource", call.receipt(), trace_ref="call-5")
    assert payload["reason"] == "running_in_background" and payload["complete"] is False
