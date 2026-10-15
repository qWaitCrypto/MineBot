"""G4: the Agent's own life profile, one JSON file per world and bot."""
import json
from types import SimpleNamespace

import pytest

from minebot.app.life_profile import (
    LifeProfileError,
    LifeRevisionConflict,
    LifeStore,
    life_summary,
    register_life_tools,
)
from minebot.app.runtime_state import RuntimeScope
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar

SCOPE = RuntimeScope("127.0.0.1:25569", "world-a", "LifeBot")
HOME = {"pos": [147, 70, 164], "dimension": "minecraft:overworld", "note": "the clearing by spawn"}
FIELD = {"name": "wheat field", "from": [150, 69, 160], "to": [156, 69, 166]}


def seed(tmp_path, value):
    path = tmp_path / "life-profile.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_seed_file_is_imported_once_as_revision_one(tmp_path):
    seed_path = seed(tmp_path, {"home": {"pos": [1, 64, 2]}, "routine": "Stay near home.", "world": "old"})
    store = LifeStore(tmp_path / "life", SCOPE, seed_path=seed_path)
    record = store.read()
    assert record["revision"] == 1
    assert record["profile"] == {"home": {"pos": [1, 64, 2]}, "routine": "Stay near home."}
    assert record["changes"][0]["author"] == "user_file"
    store.update({"routine": "Stay at home at night."}, expected_revision=1, reason="nights are dangerous")
    # A restart with the same seed keeps the Agent's own revision.
    again = LifeStore(tmp_path / "life", SCOPE, seed_path=seed_path)
    assert again.read()["revision"] == 2
    assert again.profile()["routine"] == "Stay at home at night."


def test_each_world_and_bot_has_its_own_file(tmp_path):
    first = LifeStore(tmp_path, SCOPE)
    other_world = LifeStore(tmp_path, RuntimeScope("127.0.0.1:25569", "world-b", "LifeBot"))
    first.update({"home": HOME}, expected_revision=0, reason="I picked a home")
    assert first.path != other_world.path
    assert other_world.profile() == {}
    assert first.path.name.startswith("LifeBot-")


def test_update_needs_the_current_revision_and_a_reason(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    store.update({"home": HOME}, expected_revision=0, reason="I picked a home")
    with pytest.raises(LifeRevisionConflict):
        store.update({"routine": "x"}, expected_revision=0, reason="stale")
    with pytest.raises(LifeProfileError, match="why"):
        store.update({"routine": "x"}, expected_revision=1, reason=" ")
    record = store.update({"home": None, "personality": "Quiet."}, expected_revision=1, reason="moved out")
    assert record["revision"] == 2 and "home" not in record["profile"]
    assert [change["reason"] for change in record["changes"]] == ["I picked a home", "moved out"]


@pytest.mark.parametrize("changes,message", [
    ({"home": {"pos": [1, 2]}}, "home.pos"),
    ({"relationships": [{"note": "no name"}]}, "player"),
    ({"mine": {"areas": [{"name": "a", "from": [0, 0, 0]}]}}, "to"),
    ({"projects": []}, "unknown profile fields"),
])
def test_shape_is_checked_but_content_is_not(tmp_path, changes, message):
    store = LifeStore(tmp_path, SCOPE)
    with pytest.raises(LifeProfileError, match=message):
        store.update(changes, expected_revision=0, reason="r")
    assert store.read()["revision"] == 0


def test_body_loop_reads_the_new_revision_on_the_next_frame(tmp_path):
    from minebot.app.body_loop import BodyLoop

    store = LifeStore(tmp_path, SCOPE)
    loop = BodyLoop.__new__(BodyLoop)
    loop._life_profile = {}
    loop.attach_life_store(store)
    assert loop._current_life_profile() == {}
    store.update({"routine": "Follow qwait when he is nearby."}, expected_revision=0, reason="he asked")
    assert loop._current_life_profile()["routine"] == "Follow qwait when he is nearby."


def registry_with(*tools):
    registry = ToolRegistry()
    for name, scope in tools:
        registry.register(RegisteredTool(name, name, {"type": "object"}, lambda params: None,
                                         ToolSidecar(name, mutating=True, body_scope=scope)))
    return registry


def call(registry, name, **params):
    return registry.get(name).callable(params)


def test_life_tools_read_and_write_without_touching_the_body(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    registry = registry_with()
    register_life_tools(registry, store)
    for name in ("read_life", "update_life", "update_life_project"):
        assert registry.get(name).sidecar.can_mutate_body is False
    assert call(registry, "read_life").metrics["revision"] == 0
    stale = call(registry, "update_life", changes={"home": HOME}, expected_revision=3, reason="r")
    assert stale.success is False and stale.reason == "life_update_rejected" and stale.metrics["revision"] == 0
    written = call(registry, "update_life", changes={"home": HOME}, expected_revision=0, reason="r")
    assert written.success and written.metrics["profile"]["home"] == HOME


def project(**extra):
    return {"id": "wheat-field", "intent": "wheat_seeds sown on every farmland block in the wheat field",
            "area": "wheat field",
            "work": {"kind": "call", "tool": "farm_area", "intent": "wheat_seeds 0 in the inventory",
                     "arguments": {"from": [150, 69, 160], "to": [156, 69, 166]}}, **extra}


def test_project_writes_must_stay_inside_its_own_area(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    registry = registry_with(("farm_area", ("navigation", "blocks")), ("move_to", ("navigation",)))
    register_life_tools(registry, store)
    no_area = call(registry, "update_life_project", operation="upsert", project=project(),
                   expected_revision=0, reason="start a field")
    assert no_area.success is False and "mine.areas" in no_area.metrics["error"]
    call(registry, "update_life", changes={"home": HOME, "mine": {"areas": [FIELD]}},
         expected_revision=0, reason="this field is mine")
    outside = project(work={"kind": "sequence", "intent": "field farmed", "steps": [
        {"kind": "call", "tool": "move_to", "arguments": {"pos": [400, 70, 400]}},
        {"kind": "call", "tool": "farm_area", "arguments": {"from": [150, 69, 160], "to": [170, 69, 166]}},
    ]})
    rejected = call(registry, "update_life_project", operation="upsert", project=outside,
                    expected_revision=1, reason="start a field")
    # Walking far away is not a write; farming past the area's edge is.
    assert rejected.success is False and "farm_area writes at [170.0, 69.0, 166.0]" in rejected.metrics["error"]
    accepted = call(registry, "update_life_project", operation="upsert", project=project(),
                    expected_revision=1, reason="start a field")
    assert accepted.success and accepted.metrics["projects"][0]["status"] == "active"
    paused = call(registry, "update_life_project", operation="upsert",
                  project={"id": "wheat-field", "status": "paused", "note": "North row done."},
                  expected_revision=2, reason="winter")
    assert paused.metrics["projects"][0]["status"] == "paused"
    assert paused.metrics["projects"][0]["work"]["tool"] == "farm_area"
    removed = call(registry, "update_life_project", operation="remove", project={"id": "wheat-field"},
                   expected_revision=3, reason="done with it")
    assert removed.success and removed.metrics["projects"] == []


def test_a_project_work_node_states_its_own_result(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    store.update({"mine": {"areas": [FIELD]}}, expected_revision=0, reason="mine")
    bare = project()
    bare["work"] = {key: value for key, value in bare["work"].items() if key != "intent"}
    with pytest.raises(LifeProfileError, match="work.intent is required"):
        store.update_project("upsert", bare, expected_revision=1, reason="start")


def test_unregistered_tools_cannot_enter_a_project(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    registry = registry_with()
    register_life_tools(registry, store)
    call(registry, "update_life", changes={"mine": {"areas": [FIELD]}}, expected_revision=0, reason="mine")
    result = call(registry, "update_life_project", operation="upsert", project=project(),
                  expected_revision=1, reason="start")
    assert result.success is False and "unregistered tools ['farm_area']" in result.metrics["error"]


def test_summary_tells_the_agent_where_it_lives_and_how_projects_went(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    assert "no life profile" in life_summary(store.read(), store.project_results)["note"]
    store.update({"home": HOME, "mine": {"areas": [FIELD]}}, expected_revision=0, reason="home")
    store.update_project("upsert", project(), expected_revision=1, reason="start")
    store.record_project_result("wheat-field", {"outcome": "blocked", "reason": "no seeds"})
    summary = life_summary(store.read(), store.project_results)
    assert summary["home"] == HOME and summary["mine_areas"] == ["wheat field"]
    assert summary["projects"][0]["last_result"]["outcome"] == "blocked"
    assert "work" not in summary["projects"][0]


def test_agent_context_carries_the_life_summary():
    from minebot.brain.context import AgentContext

    context = AgentContext(system_prompt="", goal_text="")
    context.observe_life({"revision": 2, "routine": "Home before dark."})
    text = context.compile("social").text
    assert 'LIFE: {"revision": 2, "routine": "Home before dark."}' in text


def test_idle_does_not_offer_calling_an_agent_that_is_already_thinking():
    from minebot.app import reflex_question as rq

    facts = {"position": [0, 64, 0], "life_profile": {"routine": "Stay home."}}
    assert "call_agent" in rq.idle_options(facts)
    assert "call_agent" not in rq.idle_options({**facts, "agent_thinking": True})


def test_a_life_hand_off_without_any_goal_wakes_the_agent():
    import asyncio
    from minebot.app.session import AgentSession, SessionCommand
    from tests.unit.test_agent_session import build_parts

    calls, bodies = [], []
    session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
    session.submit(SessionCommand.message("hello"))
    asyncio.run(session.step())
    session.parts.runtime.notify_reflex_fact({
        "call_id": "idle-loop", "activity_id": "idle-1", "kind": "life",
        "status": "handoff_requested", "reason": "decision_call_agent",
    })
    assert session.has_pending_work
    asyncio.run(session.step())
    session.close()
    assert "REFLEX_RESULT" in calls[-1] and "decision_call_agent" in calls[-1]


def test_every_direct_call_hand_off_wakes_the_agent_not_only_the_first():
    import asyncio
    from minebot.app.session import AgentSession, SessionCommand
    from tests.unit.test_agent_session import build_parts

    calls, bodies = [], []
    session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
    session.submit(SessionCommand.start("mine some stone"))
    asyncio.run(session.step())
    for call_id in ("call-a", "call-b"):
        # Direct-call hand-offs carry a call id and no activity id.
        session.parts.runtime.notify_reflex_fact({"call_id": call_id, "status": "handoff_requested"})
        assert session.has_pending_work
        asyncio.run(session.step())
        assert call_id in calls[-1]
    session.close()
