"""Smelting as a player does it: load the furnace, go on, come back for the output (user 2026-11-10)."""
from __future__ import annotations

from minebot.app.home_view import furnace_section
from minebot.app.world_record import WorldRecord, observe_tool_result
from minebot.body import FurnaceTransactions
from tests.unit.test_furnace_runtime import FakeFurnaceBody, perception, slot

FURNACE = (1, 59, 0)
HERE = {"dimension": "minecraft:overworld", "pos": [0, 59, 0]}


def _loaded_body(furnace_slots):
    return FakeFurnaceBody(
        perception("container", furnace_slots),
        perception("inventory", [slot(0, "minecraft:raw_iron", 16), slot(1, "minecraft:coal", 2), slot(2)]),
        mutable=True,
    )


def test_smelt_loads_the_furnace_and_returns_without_waiting():
    body = _loaded_body([slot(0), slot(1), slot(2)])
    result = FurnaceTransactions(body).smelt_once(
        FURNACE, input_item="minecraft:raw_iron", input_count=16, fuel_item="minecraft:coal", fuel_count=2,
        output_item="minecraft:iron_ingot", output_count=16, leave=True,
    )
    assert result.success and result.reason == "loaded"
    assert [action.params["furnace_slot"] for action in body.actions] == ["input", "fuel"]
    assert result.metrics["furnace_after"] == {"input": {"item": "raw_iron", "count": 16},
                                               "fuel": {"item": "coal", "count": 2}, "output": None}
    assert result.metrics["seconds_to_finish"] == 160
    assert "clear_furnace(pos=[1, 59, 0], take='output')" in result.next_suggestion


def test_taking_the_output_leaves_what_is_still_smelting():
    body = _loaded_body([slot(0, "minecraft:raw_iron", 11), slot(1, "minecraft:coal", 1),
                         slot(2, "minecraft:iron_ingot", 1)])
    result = FurnaceTransactions(body).clear_furnace(FURNACE, take="output")
    assert result.success
    assert [action.params["furnace_slot"] for action in body.actions] == ["output"]
    assert result.metrics["furnace_after"]["output"] is None
    assert result.metrics["furnace_after"]["input"] == {"item": "raw_iron", "count": 11}


def test_nothing_finished_yet_is_said_with_what_is_in_it():
    body = _loaded_body([slot(0, "minecraft:raw_iron", 16), slot(1, "minecraft:coal", 2), slot(2)])
    result = FurnaceTransactions(body).clear_furnace(FURNACE, take="output")
    assert not result.success and result.reason == "nothing_finished_yet"
    assert body.actions == []
    assert result.metrics["furnace_after"]["input"] == {"item": "raw_iron", "count": 16}


def _result(success, reason, metrics):
    return {"success": success, "reason": reason, "metrics": metrics}


def test_the_record_keeps_a_loaded_furnace_in_view_until_it_is_emptied(tmp_path):
    record = WorldRecord(tmp_path / "world.jsonl")
    assert furnace_section(record.snapshot(), here=HERE) == ""
    loaded = {"furnace_pos": [1, 59, 0], "furnace_type": "furnace",
              "furnace_after": {"input": {"item": "raw_iron", "count": 16}, "fuel": {"item": "coal", "count": 2},
                                "output": None}}
    observe_tool_result(record, "smelt_item", {}, _result(True, "loaded", loaded), "minecraft:overworld")
    text = furnace_section(record.snapshot(), here=HERE, now=record.snapshot()["places"][0]["t"] + 400)
    assert "FURNACES" in text
    assert "16 raw_iron with 2 coal put in 6m ago" in text
    assert "not looked into since" in text

    # Survives a restart: the record is the file, not the conversation.
    record = WorldRecord(tmp_path / "world.jsonl")
    looked = {"furnace_pos": [1, 59, 0], "furnace_type": "furnace", "moved_count": 5,
              "furnace_after": {"input": {"item": "raw_iron", "count": 11}, "fuel": {"item": "coal", "count": 1},
                                "output": None}}
    observe_tool_result(record, "clear_furnace", {}, _result(True, "completed", looked), "minecraft:overworld")
    text = furnace_section(record.snapshot(), here=HERE)
    assert "11 raw_iron not yet" in text and "put in" in text

    emptied = {"furnace_pos": [1, 59, 0], "furnace_after": {"input": None, "fuel": {"item": "coal", "count": 1},
                                                             "output": None}}
    observe_tool_result(record, "clear_furnace", {}, _result(True, "completed", emptied), "minecraft:overworld")
    assert furnace_section(record.snapshot(), here=HERE) == ""


def test_many_furnaces_side_by_side_are_one_line(tmp_path):
    record = WorldRecord(tmp_path / "world.jsonl")
    for x in range(10):
        loaded = {"furnace_pos": [x, 64, 0], "furnace_type": "furnace",
                  "furnace_after": {"input": {"item": "sand", "count": 64},
                                    "fuel": {"item": "lava_bucket", "count": 1}, "output": None}}
        observe_tool_result(record, "smelt_item", {}, _result(True, "loaded", loaded), "minecraft:overworld")
    lines = furnace_section(record.snapshot(), here=HERE).splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("- 10 furnaces around")
    assert "640 sand put in" in lines[1]


def test_waiting_by_the_furnace_stops_when_the_call_is_cancelled():
    import pytest
    from minebot.contract.execution import ExecutionCancellation, ExecutionCancelled, execution_cancellation_scope

    body = _loaded_body([slot(0), slot(1), slot(2)])
    cancellation = ExecutionCancellation()
    reads = []
    original = body.perceive

    def perceive(scope, params):
        if scope == "container" and len([a for a in body.actions]) >= 2:
            reads.append(scope)
            cancellation.cancel("decision_default_tool")
        return original(scope, params)

    body.perceive = perceive
    with execution_cancellation_scope(cancellation), pytest.raises(ExecutionCancelled):
        FurnaceTransactions(body).smelt_once(
            FURNACE, input_item="minecraft:raw_iron", input_count=16, fuel_item="minecraft:coal", fuel_count=2,
            output_item="minecraft:iron_ingot", output_count=16, poll_interval_s=0.0, smelt_timeout_s=170.0,
        )
    assert len(reads) == 1


def test_an_empty_furnace_is_said_apart_from_one_still_smelting():
    body = _loaded_body([slot(0), slot(1), slot(2)])
    result = FurnaceTransactions(body).clear_furnace(FURNACE, take="output")
    assert result.reason == "furnace_empty" and not result.can_retry
    assert body.actions == []


def test_decision_frames_say_what_the_furnaces_near_the_body_hold():
    from minebot.app import reflex_question as rq

    world = {"places": [
        {"dimension": "minecraft:overworld", "pos": [10, 86, -28], "block": "furnace",
         "furnace": {"slots": {"input": None, "fuel": None, "output": None}, "t": 1200, "last_was_load": False}},
        {"dimension": "minecraft:overworld", "pos": [12, 86, -28], "block": "furnace",
         "furnace": {"slots": {"input": {"item": "sand", "count": 20}, "fuel": {"item": "coal", "count": 3},
                               "output": None}, "t": 1100, "last_was_load": True,
                     "loaded": {"t": 1100, "slots": {"input": {"item": "sand", "count": 20},
                                                     "fuel": {"item": "coal", "count": 3}}}}},
        {"dimension": "minecraft:overworld", "pos": [200, 86, -28], "block": "furnace",
         "furnace": {"slots": {"input": {"item": "sand", "count": 1}}, "t": 1100, "last_was_load": True}},
    ]}
    furnaces = rq.known_furnaces(world, "minecraft:overworld", [11.5, 86, -25.5], 1240)
    assert [item["pos"] for item in furnaces] == [[10, 86, -28], [12, 86, -28]]
    text = rq.life_surroundings({"known_furnaces": furnaces})["furnaces"]
    assert "furnace at (10, 86, -28), 2 blocks away: empty, looked into less than a minute ago" in text
    assert "20 sand with 3 coal put in 2 minutes ago, not looked into since" in text
    assert "furnaces" not in rq.life_surroundings({})
