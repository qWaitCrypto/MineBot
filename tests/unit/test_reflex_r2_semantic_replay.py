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
    assert len(rows) == 10
    assert {row["expected"] for row in rows} >= {"continue", "step_done", "call_agent"}
    assert {row["id"] for row in rows if row["previous"] != row["expected"]} >= {
        "smelt_approaching", "build_wrong_material", "build_coal_enough"
    }
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
        request = supervisor._question(
            row["id"], tool, row["params"], {
                "operation_id": row["id"], "tool": row["tool"], "state": "running",
                "phase": row["frame"]["phase"], "elapsed_s": row["frame"]["elapsed_s"],
                "fact_revision": 1, "facts": row["frame"]["facts"],
            }, idle_s=0.0, purpose="replay",
            supervision_context={
                "goal": intent, "current_step": intent, "node_description": intent,
                "intent": intent, "is_plan": False,
            },
        )
        encoded = json.dumps(request, ensure_ascii=False)
        assert request["state"]["plan"]["intent"] == intent
        assert intent in encoded
        assert "state.plan." not in encoded
        assert "<bounded>" not in encoded
        assert row["expected"] in request["questions"]["q"]["criteria"]
    assert len(json.dumps(request, ensure_ascii=False)) < 3000


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
