import json
from pathlib import Path

from minebot.app.direct_call_supervision import CallSupervisor


def test_semantic_replay_fixture_preserves_the_three_known_mistakes():
    fixture = Path(__file__).parents[1] / "fixtures" / "reflex_r2_semantic_replay.json"
    rows = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(rows) == 10
    assert [row["id"] for row in rows if not row["expected"]] == [
        "smelt_approaching", "build_wrong_material", "build_coal_enough"
    ]
    assert all(row["facts"] and "<bounded>" not in row["facts"] for row in rows)


def test_semantic_supervision_criteria_use_meaning_without_state_pointers():
    plan = {
        "goal": "gather materials for a house",
        "current_step": "collect oak logs",
        "node_description": "collect 5 oak logs for the house foundation",
        "intent_chain": ["prepare materials"],
    }
    criteria = CallSupervisor._criteria(plan, tool_name="collect_resource")
    encoded = json.dumps(criteria, ensure_ascii=False)
    assert "state.plan" not in encoded
    assert "collect 5 oak logs" in encoded
    assert "continue by default" in criteria["continue"]
    assert "facts positively" in criteria["step_done"]
    assert "positive" in criteria["call_agent"]
    assert all(len(value) < 400 for value in criteria.values())
