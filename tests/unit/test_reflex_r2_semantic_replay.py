import json
from pathlib import Path
from types import SimpleNamespace

from minebot.app.direct_call_supervision import CallSupervisor
from minebot.brain.registry import RegisteredTool, ToolSidecar
from minebot.contract import ToolResult


def _rows():
    fixture = Path(__file__).parents[1] / "fixtures" / "reflex_r2_semantic_replay.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_replay_fixture_contains_raw_frames_intents_and_non_continue_answers():
    rows = _rows()
    assert len(rows) >= 28
    assert {row["expected"] for row in rows} >= {"continue", "step_done", "call_agent"}
    assert {row["id"] for row in rows if row["previous"] != row["expected"]} >= {
        "smelt_approaching", "build_wrong_material", "build_coal_enough"
    }
    assert {
        "real3_move_start_1", "real3_move_start_2",
        "real3_get_to_block_start", "real3_explore_for_start",
        "real7_smelt_loaded", "real7_collect_after_edit", "real7_water_escape",
        "real8_water_oxygen", "real8_hungry_food", "real8_hostile_near", "real8_hostile_far",
        "real8_failure_history",
    } <= {row["id"] for row in rows}
    assert all(row["intent"] and isinstance(row["frame"]["facts"], dict) for row in rows)


def test_replay_frames_go_through_the_real_question_builder():
    runtime = SimpleNamespace(
        agent_context=None,
        native_task_workspace=None,
        decision_client=None,
        trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
    )
    supervisor = CallSupervisor(runtime)
    for row in _rows():
        tool = SimpleNamespace(
            name=row["tool"],
            supervision_explanation="The Body approaches the relevant context, performs the requested operation, and verifies the terminal result.",
        )
        intent = row["intent"]
        execution = {
            "operation_id": row["id"], "tool": row["tool"], "state": "running",
            "phase": row["frame"]["phase"], "elapsed_s": row["frame"]["elapsed_s"],
            "fact_revision": 1, "facts": row["frame"]["facts"],
        }
        context = {
            "goal": intent, "current_step": intent, "node_description": intent,
            "intent": intent, "is_plan": False,
        }
        options = supervisor._options_for(
            tool, {"tool": row["tool"], "params": row["params"],
                   "supervision_context": context, "reader_new_fact": False},
            execution=execution,
        )
        request = supervisor._question(
            row["id"], tool, row["params"], execution,
            idle_s=0.0, purpose="replay", options=options,
            supervision_context=context,
        )
        encoded = json.dumps(request, ensure_ascii=False)
        assert request["state"]["agent_intent"] == intent
        assert "state.plan." not in encoded
        assert "<bounded>" not in encoded
        expected_key = {"continue": "keep_going", "step_done": "next_step", "go_to_surface": "surface"}.get(row["expected"], row["expected"])
        if expected_key == "surface":
            assert {"keep_going", "next_step", "call_agent"} <= {
                k for q in request["questions"].values() for k in q["criteria"]
            }
        else:
            assert expected_key in {k for q in request["questions"].values() for k in q["criteria"]}
    assert len(json.dumps(request, ensure_ascii=False)) < 3000


def test_failure_replay_uses_result_shape_and_recent_history():
    row = next(row for row in _rows() if row["id"] == "real8_failure_history")
    runtime = SimpleNamespace(
        agent_context=None, native_task_workspace=None, native_plan=None,
        decision_client=None, trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
    )
    supervisor = CallSupervisor(runtime)
    tool = SimpleNamespace(name=row["tool"], supervision_reader=None,
                           supervision_explanation="Approach, place, and verify the crafted result.")
    request = supervisor._question(
        row["id"], tool, row["params"], {"tool": row["tool"], "state": "succeeded",
        "phase": "terminal", "facts": row["frame"]["facts"]}, idle_s=0.0,
        purpose="replay_result", options={"continue": "continue", "step_done": "done", "call_agent": "agent"},
        supervision_context={"intent": row["intent"]},
        decision_context={"tool_arguments": row["params"], "tool_result": row["result"],
                          "body_state": row["frame"]["facts"], "history": row["history"], "reads": []},
    )
    payload = json.dumps(request, ensure_ascii=False)
    assert "finished_call" in request["state"]
    assert "crafting_table_place_failed" in payload
    assert len(request["state"].get("earlier_attempts_of_this_call", [])) >= 5
    assert "elapsed 0s" not in payload.lower()


def test_replay_compiler_has_complete_unique_options_and_applicability_filters():
    runtime = SimpleNamespace(
        agent_context=None, native_task_workspace=None, native_plan=None,
        decision_client=None, trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
    )
    supervisor = CallSupervisor(runtime)
    rows = _rows()
    near = next(row for row in rows if row["id"] == "real8_hostile_near")
    far = next(row for row in rows if row["id"] == "real8_hostile_far")
    near_options = supervisor._options_for(
        SimpleNamespace(name=near["tool"], supervision_reader=None), {"reader_new_fact": False},
        execution={"facts": near["frame"]["facts"]},
    )
    far_options = supervisor._options_for(
        SimpleNamespace(name=far["tool"], supervision_reader=None), {"reader_new_fact": False},
        execution={"facts": far["frame"]["facts"]},
    )
    assert "move_away" in near_options
    assert "move_away" not in far_options
    assert len(set(near_options)) == len(near_options)
    assert {"keep_going", "next_step", "call_agent", "move_away"} <= set(near_options)


def test_replay_does_not_invent_derived_quantity_facts():
    runtime = SimpleNamespace(
        agent_context=None, native_task_workspace=None, native_plan=None,
        decision_client=None, trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
    )
    supervisor = CallSupervisor(runtime)
    for row_id in ("craft_enough", "build_coal_enough"):
        row = next(row for row in _rows() if row["id"] == row_id)
        request = supervisor._question(
            row["id"], SimpleNamespace(name=row["tool"], supervision_reader=None), row["params"],
            {"tool": row["tool"], "state": "running", "phase": row["frame"]["phase"],
             "elapsed_s": 1.0, "facts": row["frame"]["facts"]},
            idle_s=0.0, purpose="replay", supervision_context={"intent": row["intent"]},
        )
        encoded = json.dumps(request, ensure_ascii=False)
        assert "remaining quantity is" not in encoded


def test_body_mutating_tool_schema_exposes_optional_agent_intent():
    tool = RegisteredTool(
        name="move_to",
        description="Move the Body.",
        input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
        callable=lambda _args: ToolResult(True, "completed"),
        sidecar=ToolSidecar(progress_key="move", mutating=True),
    )
    schema = tool.framework_view()["input_schema"]
    assert "intent" in schema["properties"]
    assert "intent" not in schema.get("required", [])
