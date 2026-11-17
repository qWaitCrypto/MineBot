"""G4: the Agent's own life profile, one JSON file per world and bot."""
import json
from types import SimpleNamespace

import pytest

from minebot.app.life_profile import (
    LifeProfileError,
    LifeProfileUnreadable,
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
    assert loop.current_life_profile() == {}
    store.update({"routine": "Follow qwait when he is nearby."}, expected_revision=0, reason="he asked")
    assert loop.current_life_profile()["routine"] == "Follow qwait when he is nearby."


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


@pytest.mark.parametrize("field", ["routine", "personality"])
def test_life_text_rejection_reports_submitted_length_and_schema_limit(tmp_path, field):
    from minebot.app.runner import _model_tool_payload

    registry = registry_with()
    register_life_tools(registry, LifeStore(tmp_path, SCOPE))
    tool = registry.get("update_life")
    result = call(registry, tool.name, changes={field: "木" * 1180}, expected_revision=0, reason="r")
    shown = _model_tool_payload(tool.name, result.to_payload(), trace_ref="length", projector=tool.projector)
    assert shown["summary"]["error"] == f"{field} is 1180 characters; the limit is 1000"
    assert tool.input_schema["properties"]["changes"]["properties"][field]["maxLength"] == 1000


def test_project_note_rejection_reports_submitted_length_and_schema_limit(tmp_path):
    from minebot.app.runner import _model_tool_payload

    registry = registry_with()
    register_life_tools(registry, LifeStore(tmp_path, SCOPE))
    tool = registry.get("update_life_project")
    result = call(registry, tool.name, operation="upsert", project=project(note="x" * 1180),
                  expected_revision=0, reason="r")
    shown = _model_tool_payload(tool.name, result.to_payload(), trace_ref="length", projector=tool.projector)
    assert shown["summary"]["error"] == (
        "wheat-field.note is 1180 characters; the limit is 1000. why and note are yours alone. How each run "
        "went is kept in the item's account (LADDER, read_life); what you learned goes in write_memory.")
    assert tool.input_schema["properties"]["project"]["properties"]["note"]["maxLength"] == 1000


@pytest.mark.parametrize("field,length,limit,where", [
    ("id", 49, 48, None), ("intent", 301, 300, "write_memory"), ("when", 301, 300, "write_memory"),
    ("note", 1001, 1000, "account"), ("why", 1001, 1000, "account")])
def test_other_project_text_limits_are_declared_and_reported(tmp_path, field, length, limit, where):
    # S7: intents grew to 986 characters of evidence, and notes overflowed
    # 1000 sixteen times (s7-later-20261029.md §8 D7).
    store = LifeStore(tmp_path, SCOPE)
    store.update({"mine": {"areas": [FIELD]}}, expected_revision=0, reason="mine")
    registry = registry_with(("farm_area", ("navigation", "blocks")))
    register_life_tools(registry, store)
    tool = registry.get("update_life_project")
    result = call(registry, tool.name, operation="upsert", project=project(**{field: "x" * length}),
                  expected_revision=1, reason="r")
    name = "project.id" if field == "id" else f"wheat-field.{field}"
    assert result.metrics["error"].startswith(f"{name} is {length} characters; the limit is {limit}")
    schema = tool.input_schema["properties"]["project"]["properties"][field]
    assert schema["maxLength"] == limit
    if where:
        # The refusal and the schema both say where longer writing goes.
        assert where in result.metrics["error"] and where in schema["description"]


def test_an_item_written_before_the_sentence_limit_keeps_its_intent_until_rewritten(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    store.update_project("upsert", {"id": "old", "intent": "x" * 299}, expected_revision=0, reason="r")
    record = store.read()
    record["profile"]["projects"][0]["intent"] = "x" * 900  # as S7 left it
    store._write(record)
    # Other writes go through; rewriting the old item's intent must be one sentence.
    store.update({"routine": "Home before dark."}, expected_revision=1, reason="r")
    store.update_project("upsert", {"id": "old", "note": "still long"}, expected_revision=2, reason="r")
    with pytest.raises(LifeProfileError, match="old.intent is 400 characters; the limit is 300"):
        store.update_project("upsert", {"id": "old", "intent": "y" * 400}, expected_revision=3, reason="r")
    with pytest.raises(LifeProfileError, match="goal is 301 characters"):
        store.put_delegated({"goal": "g" * 301, "timeout_s": 60, "work": WALK})


@pytest.mark.parametrize("changes,field,schema_path", [
    ({"home": {"pos": [0, 64, 0], "note": "x" * 1001}}, "home.note", ("home", "properties", "note")),
    ({"home": {"pos": [0, 64, 0], "dimension": "x" * 1001}}, "home.dimension",
     ("home", "properties", "dimension")),
    ({"mine": {"areas": [{"name": "x" * 1001}]}}, "mine.areas[0].name",
     ("mine", "properties", "areas", "items", "properties", "name")),
    ({"mine": {"areas": [{**FIELD, "dimension": "x" * 1001}]}}, "mine.areas[0].dimension",
     ("mine", "properties", "areas", "items", "properties", "dimension")),
    ({"relationships": [{"player": "x" * 1001}]}, "relationships[0].player",
     ("relationships", "items", "properties", "player")),
    ({"relationships": [{"player": "qwait", "note": "x" * 1001}]}, "relationships[0].note",
     ("relationships", "items", "properties", "note")),
])
def test_nested_life_text_fields_declare_their_existing_limits(tmp_path, changes, field, schema_path):
    registry = registry_with()
    register_life_tools(registry, LifeStore(tmp_path, SCOPE))
    tool = registry.get("update_life")
    schema = tool.input_schema["properties"]["changes"]["properties"]
    for key in schema_path:
        schema = schema[key]
    assert schema["maxLength"] == 1000
    result = call(registry, tool.name, changes=changes, expected_revision=0, reason="r")
    assert result.metrics["error"] == f"{field} is 1001 characters; the limit is 1000"


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


def test_summary_tells_the_agent_where_it_lives_and_leaves_the_ladder_to_ladder(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    assert "no life profile" in life_summary(store.read())["note"]
    store.update({"home": HOME, "mine": {"areas": [FIELD]}}, expected_revision=0, reason="home")
    store.update_project("upsert", project(), expected_revision=1, reason="start")
    store.record_project_result("wheat-field", {"outcome": "blocked", "reason": "no seeds"})
    summary = life_summary(store.read())
    assert summary["home"] == HOME and summary["mine_areas"] == ["wheat field"]
    # One list of the items, in LADDER (s7-later-20261029.md §8 D4).
    assert "projects" not in summary


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


# -- plan ladder S1 (plan-ladder-20261028.md §7.1) ----------------------------


@pytest.mark.parametrize("text", ["{not json", "[]", '{"revision": 3}', '{"revision": {"n": 1}, "profile": {}}',
                                  '{"revision": 1, "profile": {}, "changes": 5}'])
def test_an_unreadable_profile_raises_and_is_never_overwritten(tmp_path, text):
    store = LifeStore(tmp_path, SCOPE)
    store.path.write_text(text, encoding="utf-8")
    with pytest.raises(LifeProfileUnreadable):
        store.read()
    with pytest.raises(LifeProfileUnreadable):
        store.update({"routine": "x"}, expected_revision=0, reason="r")
    assert store.path.read_text(encoding="utf-8") == text


def test_a_seed_is_not_imported_over_an_unreadable_profile(tmp_path):
    first = LifeStore(tmp_path / "life", SCOPE)
    first.path.parent.mkdir(parents=True)
    first.path.write_text("{broken", encoding="utf-8")
    LifeStore(tmp_path / "life", SCOPE, seed_path=seed(tmp_path, {"routine": "Seed."}))
    assert first.path.read_text(encoding="utf-8") == "{broken"


def test_body_loop_keeps_the_last_profile_when_the_file_breaks(tmp_path):
    from minebot.app.body_loop import BodyLoop

    store = LifeStore(tmp_path, SCOPE)
    store.update({"routine": "Stay near home."}, expected_revision=0, reason="r")
    events = []
    loop = BodyLoop.__new__(BodyLoop)
    loop._life_profile = {}
    loop.runtime = SimpleNamespace(trace=SimpleNamespace(emit=lambda name, **fields: events.append(name)))
    loop.attach_life_store(store)
    store.path.write_text("{broken!", encoding="utf-8")
    assert loop.current_life_profile()["routine"] == "Stay near home."
    assert events == ["life_profile_unreadable"]


def test_ladder_items_think_look_walk_need_only_id_and_intent(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    store.update({"mine": {"areas": [FIELD]}}, expected_revision=0, reason="mine")
    store.update_project("upsert", {"id": "stronghold", "intent": "Find the stronghold.",
                                    "why": "需要末地门"}, expected_revision=1, reason="think")
    store.update_project("upsert", {"id": "nether-portal", "intent": "Build a nether portal.",
                                    "when": "it is daytime"}, expected_revision=2, reason="look")
    walk = project(area=None, standing=True, when="it is daytime and wheat_seeds are in the inventory")
    record = store.update_project("upsert", walk, expected_revision=3, reason="walk")
    items = {item["id"]: item for item in record["profile"]["projects"]}
    assert items["stronghold"] == {"id": "stronghold", "intent": "Find the stronghold.",
                                   "why": "需要末地门", "status": "active"}
    assert "work" not in items["nether-portal"] and items["nether-portal"]["when"] == "it is daytime"
    assert "area" not in items["wheat-field"] and items["wheat-field"]["standing"] is True
    with pytest.raises(LifeProfileError, match="standing"):
        store.update_project("upsert", {"id": "x", "intent": "y", "standing": "yes"},
                             expected_revision=4, reason="r")
    with pytest.raises(LifeProfileError, match="area"):
        store.update_project("upsert", {"id": "x", "intent": "y", "area": "nowhere"},
                             expected_revision=4, reason="r")


def test_before_orders_the_ladder(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    revision = 0
    for name in ("a", "b", "c"):
        revision = store.update_project("upsert", {"id": name, "intent": name}, expected_revision=revision,
                                        reason="add")["revision"]
    ids = lambda record: [item["id"] for item in record["profile"]["projects"]]  # noqa: E731
    record = store.update_project("upsert", {"id": "d", "intent": "d"}, expected_revision=revision,
                                  reason="first", before="a")
    assert ids(record) == ["d", "a", "b", "c"]
    record = store.update_project("upsert", {"id": "c", "note": "now"}, expected_revision=record["revision"],
                                  reason="move", before="a")
    assert ids(record) == ["d", "c", "a", "b"]
    record = store.update_project("upsert", {"id": "a", "note": "stays"}, expected_revision=record["revision"],
                                  reason="keep")
    assert ids(record) == ["d", "c", "a", "b"]
    for before, message in (("zz", "no project"), ("b", "another project")):
        with pytest.raises(LifeProfileError, match=message):
            store.update_project("upsert", {"id": "b", "intent": "b"}, expected_revision=record["revision"],
                                 reason="r", before=before)


def test_the_ladder_holds_32_live_items(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    for index in range(32):
        store.update_project("upsert", {"id": f"item-{index}", "intent": "x" * 300},
                             expected_revision=index, reason="add")
    with pytest.raises(LifeProfileError, match="leave by themselves"):
        store.update_project("upsert", {"id": "one-more", "intent": "x"}, expected_revision=32, reason="add")
    # An item marked done leaves at once and makes room.
    record = store.update_project("upsert", {"id": "item-0", "status": "done"}, expected_revision=32, reason="done")
    assert len(record["profile"]["projects"]) == 31
    record = store.update_project("upsert", {"id": "one-more", "intent": "x"}, expected_revision=33, reason="add")
    assert len(record["profile"]["projects"]) == 32


WALK = {"kind": "call", "tool": "fake", "arguments": {}, "intent": "x"}


def test_finished_items_leave_the_ladder_with_the_agents_next_write(tmp_path):
    # S7 2026-10-28: at the hand-back 28 of the 32 items were done and the
    # ladder was full (s7-later-20261029.md §8 D5).
    store = LifeStore(tmp_path, SCOPE)
    revision = 0
    for item in ({"id": "camp-kit", "intent": "Sleep in the bed.", "work": WALK},
                 {"id": "arrows", "intent": "Make arrows.", "work": WALK},
                 {"id": "farm", "intent": "Harvest.", "work": WALK, "standing": True},
                 {"id": "idea", "intent": "Find the stronghold."}):
        revision = store.update_project("upsert", item, expected_revision=revision, reason="add")["revision"]
    for name in ("camp-kit", "arrows", "farm"):
        store.record_project_event(name, "started", who="decision")
    store.record_project_result("camp-kit", {"outcome": "completed"})
    store.record_project_result("arrows", {"outcome": "completed", "reported_steps": [
        {"node": "n1", "tool": "craft_item", "result": "missing_ingredients"}]})
    store.record_project_result("farm", {"outcome": "completed"})
    items = {item["id"]: item for item in store.profile()["projects"]}
    # Off the ladder at once; one that completed past a reported step stays, and so does a standing one.
    assert store.project_state(items["camp-kit"])["off_ladder"] == "completed"
    assert "off_ladder" not in store.project_state(items["arrows"])
    assert "off_ladder" not in store.project_state(items["farm"])
    assert store.read()["revision"] == revision  # nothing moves the revision but the Agent
    record = store.update_project("upsert", {"id": "idea", "status": "done"}, expected_revision=revision,
                                  reason="found it by hand")
    assert [item["id"] for item in record["profile"]["projects"]] == ["arrows", "farm"]
    assert {row["id"]: row["why"] for row in store.finished_items()} == {
        "camp-kit": "completed", "idea": "done_by_you"}
    # Written again, a finished item comes back as it was, with its history.
    record = store.update_project("upsert", {"id": "camp-kit", "note": "the bed moved"},
                                  expected_revision=record["revision"], reason="sleep there again")
    item = next(item for item in record["profile"]["projects"] if item["id"] == "camp-kit")
    assert item["intent"] == "Sleep in the bed." and item["work"] == WALK and item["status"] == "active"
    assert store.project_state(item)["state"] == "waiting"
    assert store.project_results("camp-kit")[-1]["outcome"] == "completed"
    assert {row["id"] for row in store.finished_items()} == {"idea"}
    # The log keeps the finished items across a restart.
    again = LifeStore(tmp_path, SCOPE)
    assert [row["id"] for row in again.finished_items()] == ["idea"]
    record = again.update_project("upsert", {"id": "idea"}, expected_revision=record["revision"], reason="back")
    assert next(item for item in record["profile"]["projects"] if item["id"] == "idea")["status"] == "active"


def test_each_item_keeps_an_account_of_all_its_runs(tmp_path):
    # S7: diamond-tier ran 6 times and was handed back 6 times, and the Agent
    # kept that account in its intents (s7-later-20261029.md §8 D6).
    store = LifeStore(tmp_path, SCOPE)
    item = {"id": "diamond-tier", "intent": "Get diamonds.", "work": WALK}
    revision = store.update_project("upsert", item, expected_revision=0, reason="r")["revision"]
    stop = {"node": "work.steps[1]", "tool": "equip_item", "result": "item_not_available",
            "why": "decision_call_agent"}
    for duration in (150.0, 210.5):
        store.record_project_event("diamond-tier", "started", who="decision")
        store.record_project_result("diamond-tier", {"outcome": "needs_agent", "duration_s": duration,
                                                     "stopped_at": stop, "inventory_change": {}})
        revision = store.update_project("upsert", {**item, "note": "again"}, expected_revision=revision,
                                        reason="again")["revision"]
    account = store.project_account("diamond-tier")
    assert account["runs"] == 2 and account["seconds"] == 360.5 and account["rewrites"] == 2
    assert account["outcomes"] == {"needs_agent": 2} and account["same_end_twice"] == stop
    assert account["ends"][-1]["inventory_change"] == {}
    # Decision's frames never carry it: the run state has no account.
    assert "account" not in store.project_state(item)
    # A different end breaks the pair; the account outlives a restart.
    store.record_project_event("diamond-tier", "started", who="decision")
    store.record_project_result("diamond-tier", {"outcome": "blocked", "reason": "node_timeout", "duration_s": 30})
    account = LifeStore(tmp_path, SCOPE).project_account("diamond-tier")
    assert account["runs"] == 3 and account["outcomes"] == {"needs_agent": 2, "blocked": 1}
    assert "same_end_twice" not in account and account["rewrites"] == 2
    assert store.project_account("nothing") == {"runs": 0, "seconds": 0.0, "outcomes": {}, "ends": [], "rewrites": 0}


def test_read_life_shows_finished_items_apart_from_the_ladder(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    registry = registry_with()
    register_life_tools(registry, store)
    store.update_project("upsert", {"id": "a", "intent": "a", "work": WALK}, expected_revision=0, reason="r")
    store.update_project("upsert", {"id": "b", "intent": "b"}, expected_revision=1, reason="r")
    store.record_project_event("a", "started", who="decision")
    store.record_project_result("a", {"outcome": "completed", "position": [1.0, 64.0, 2.0]})
    read = call(registry, "read_life")
    assert [item["id"] for item in read.metrics["profile"]["projects"]] == ["b"]
    assert read.metrics["project_states"] == {"b": "waiting"} and list(read.metrics["project_results"]) == ["b"]
    finished = read.metrics["finished_items"]
    assert [(row["id"], row["why"]) for row in finished] == [("a", "completed")]
    assert finished[0]["last_result"]["position"] == [1.0, 64.0, 2.0]
    assert finished[0]["account"]["runs"] == 1 and read.metrics["project_accounts"]["b"]["runs"] == 0
    tool = registry.get("update_life_project")
    assert "leaves the ladder by itself" in tool.description and "brings it back" in tool.description


def test_update_life_project_tool_takes_before_and_the_new_fields(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    registry = registry_with()
    register_life_tools(registry, store)
    call(registry, "update_life_project", operation="upsert", project={"id": "a", "intent": "a"},
         expected_revision=0, reason="r")
    result = call(registry, "update_life_project", operation="upsert",
                  project={"id": "b", "intent": "b", "why": "w", "when": "it is day", "standing": True},
                  before="a", expected_revision=1, reason="r")
    assert result.success, result.metrics
    assert [item["id"] for item in result.metrics["projects"]] == ["b", "a"]
    schema = registry.get("update_life_project").input_schema
    assert {"why", "when", "standing"} <= set(schema["properties"]["project"]["properties"])
    assert "before" in schema["properties"]
    store.path.write_text("{broken", encoding="utf-8")
    result = call(registry, "update_life_project", operation="upsert", project={"id": "a", "intent": "a"},
                  expected_revision=2, reason="r")
    assert result.success is False and result.reason == "life_profile_unreadable"


def test_run_state_is_read_from_the_project_log(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    item = {"id": "mine-iron", "intent": "x"}
    state = lambda: store.project_state(item)["state"]  # noqa: E731
    assert state() == "waiting"
    store.record_project_event("mine-iron", "started", who="decision")
    assert state() == "running"
    # An Agent change does not stop a running item.
    store.update_project("upsert", item, expected_revision=0, reason="reword")
    assert state() == "running"
    store.record_project_result("mine-iron", {"outcome": "blocked", "reason": "target_not_found"})
    assert state() == "set_aside"
    assert store.project_state(item)["last_result"]["reason"] == "target_not_found"
    store.update_project("upsert", item, expected_revision=1, reason="fixed the search")
    assert state() == "waiting"
    store.record_project_event("mine-iron", "started", who="decision")
    store.record_project_result("mine-iron", {"outcome": "yielded"})
    assert state() == "waiting"
    store.record_project_event("mine-iron", "started", who="agent")
    store.record_project_result("mine-iron", {"outcome": "completed"})
    assert state() == "done"
    assert store.project_state({**item, "standing": True})["state"] == "waiting"
    assert store.project_state(item)["ended_last"] is True
    store.update_project("upsert", item, expected_revision=2, reason="reword")
    assert store.project_state(item)["ended_last"] is False
    # Results shown to the Agent are outcomes only, not run events.
    assert [row["outcome"] for row in store.project_results("mine-iron")] == ["blocked", "yielded", "completed"]


def test_a_restart_returns_a_cut_off_item_to_waiting(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    store.record_project_event("mine-iron", "started", who="decision")
    store.record_project_event("farm", "started", who="decision")
    store.record_project_result("farm", {"outcome": "completed"})
    again = LifeStore(tmp_path, SCOPE)
    assert again.project_state({"id": "mine-iron"})["state"] == "waiting"
    assert again.project_state({"id": "farm"})["state"] == "done"
    rows = [json.loads(line) for line in again.log_path.read_text(encoding="utf-8").splitlines()]
    assert [row.get("event") for row in rows if row["project"] == "mine-iron"] == ["started", "restart"]
    # A second open finds nothing cut off.
    LifeStore(tmp_path, SCOPE)
    assert len(again.log_path.read_text(encoding="utf-8").splitlines()) == len(rows)


def test_frames_carry_the_ladder_run_states_and_the_hold(tmp_path):
    from minebot.app.body_loop import BodyLoop

    store = LifeStore(tmp_path, SCOPE)
    store.update_project("upsert", {"id": "a", "intent": "a"}, expected_revision=0, reason="r")
    store.record_project_event("a", "started", who="decision")
    from tests.unit.test_body_loop import _runtime

    runtime, _trace, _decision = _runtime()
    held = {"value": False}
    runtime.reflex_manager = SimpleNamespace(ladder_held=lambda: held["value"])
    loop = BodyLoop(runtime)
    loop.attach_life_store(store)
    facts = loop._frame({"facts": {"position": [0, 64, 0]}})["facts"]
    assert facts["ladder_states"] == {"a": "running"} and "ladder_held" not in facts
    held["value"] = True
    assert loop._frame({"facts": {"position": [0, 64, 0]}})["facts"]["ladder_held"] is True


def test_a_log_that_cannot_be_written_does_not_fail_a_committed_change(tmp_path, monkeypatch):
    store = LifeStore(tmp_path, SCOPE)
    store.update_project("upsert", {"id": "farm", "intent": "Harvest the field.", "work": {"kind": "call",
                         "tool": "farm_area", "arguments": {}, "intent": "wheat in the inventory"}},
                         expected_revision=0, reason="start")
    store.record_project_event("farm", "started", who="decision")
    assert store.project_state(store.profile()["projects"][0])["state"] == "running"

    def broken(row):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_append", broken)
    record = store.update_project("upsert", {"id": "farm", "intent": "Harvest the whole field."},
                                 expected_revision=1, reason="change")
    assert record["revision"] == 2
    item = store.profile()["projects"][0]
    assert item["intent"] == "Harvest the whole field."
    assert store.project_state(item)["state"] == "running"  # a change during a run does not stop it


def test_a_result_cannot_rename_its_project(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    store.record_project_result("farm", {"outcome": "completed", "project": "other"})
    assert [row["project"] for row in store.project_results("farm")] == ["farm"]


def test_an_item_cancelled_by_the_session_going_away_waits_again(tmp_path):
    # S6 live check 2026-10-28: a quit before a restart ended the running
    # item as cancelled, and it stayed set aside after the restart.
    store = LifeStore(tmp_path, SCOPE)
    item = {"id": "shelter"}
    store.record_project_event("shelter", "started", who="decision")
    store.record_project_result("shelter", {"outcome": "cancelled",
                                            "reason": "generation_invalidated:session_command:quit"})
    assert store.project_state(item)["state"] == "waiting"
    store.record_project_event("shelter", "started", who="decision")
    store.record_project_result("shelter", {"outcome": "cancelled", "reason": "reflex_cancel"})
    assert store.project_state(item)["state"] == "set_aside"
    # take_control puts the running item down (§7.2): it does not wait again.
    store.record_project_event("shelter", "started", who="decision")
    store.record_project_result("shelter", {"outcome": "cancelled",
                                            "reason": "generation_invalidated:reflex_take_control"})
    assert store.project_state(item)["state"] == "set_aside"



# -- home, outposts and belongings (vision-and-home-20261028.md §7.7) ----------

STORAGE = [{"pos": [148, 70, 160], "holds": ["minecraft:cobblestone", "*_log"], "note": "by the door"},
           {"pos": [149, 70, 160], "holds": ["iron_ingot"]}]


def test_home_takes_a_name_an_area_and_a_storage_plan(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    record = store.update({"home": {**HOME, "name": "the clearing house", "dimension": "overworld",
                                    "area": {"from": [140, 60, 155], "to": [155, 80, 170]},
                                    "storage": STORAGE}}, expected_revision=0, reason="I sorted my chests")
    home = record["profile"]["home"]
    assert home["name"] == "the clearing house" and home["dimension"] == "minecraft:overworld"
    assert home["area"] == {"from": [140, 60, 155], "to": [155, 80, 170]}
    assert home["storage"][0] == {"pos": [148, 70, 160], "holds": ["cobblestone", "*_log"], "note": "by the door"}


@pytest.mark.parametrize("home,message", [
    ({**HOME, "storage": [{"pos": [1, 2, 3], "holds": []}]}, r"home.storage\[0\].holds must be a list of 1 to 32"),
    ({**HOME, "storage": [{"pos": [1, 2, 3], "holds": ["iron ingots"]}]}, r"home.storage\[0\].holds\[0\]"),
    ({**HOME, "storage": [{"pos": [1, 2, 3], "holds": ["coal"]}, {"pos": [1, 2, 3], "holds": ["stone"]}]},
     "two rows for"),
    ({**HOME, "area": {"from": [1, 2, 3]}}, "home.area.to"),
    ({**HOME, "dimension": "the nether"}, "home.dimension must be overworld, the_nether or the_end"),
])
def test_home_storage_and_dimension_shapes_are_checked(tmp_path, home, message):
    store = LifeStore(tmp_path, SCOPE)
    with pytest.raises(LifeProfileError, match=message):
        store.update({"home": home}, expected_revision=0, reason="r")


def test_outposts_need_a_unique_name_and_a_dimension(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    nether = {"name": "nether base", "pos": [20, 70, 20], "dimension": "nether", "storage": STORAGE[:1]}
    record = store.update({"outposts": [nether]}, expected_revision=0, reason="a base by the portal")
    assert record["profile"]["outposts"][0]["dimension"] == "minecraft:the_nether"
    with pytest.raises(LifeProfileError, match="same name"):
        store.update({"outposts": [nether, nether]}, expected_revision=1, reason="r")
    with pytest.raises(LifeProfileError, match=r"outposts\[0\].dimension is needed"):
        store.update({"outposts": [{"name": "x", "pos": [0, 64, 0]}]}, expected_revision=1, reason="r")
    with pytest.raises(LifeProfileError, match="at most 8"):
        store.update({"outposts": [{**nether, "name": str(index)} for index in range(9)]},
                     expected_revision=1, reason="r")


def test_belongings_name_owner_place_and_who_said_so(tmp_path, monkeypatch):
    store = LifeStore(tmp_path, SCOPE)
    chest = {"owner": "qwait", "pos": [150, 70, 160], "dimension": "overworld", "said_by": "qwait"}
    shared = {"owner": "shared", "area": {"from": [0, 60, 0], "to": [9, 70, 9]}, "dimension": "overworld",
              "said_by": "qwait", "said_at": "day 3, at dusk", "note": "the farm we share"}
    monkeypatch.setattr("minebot.app.life_profile._now", lambda: "2026-10-28T20:00:00Z")
    record = store.update({"belongings": [chest, shared]}, expected_revision=0, reason="qwait told me")
    first, second = record["profile"]["belongings"]
    assert first == {**chest, "dimension": "minecraft:overworld", "said_at": "2026-10-28T20:00:00Z"}
    assert second["said_at"] == "day 3, at dusk" and second["area"] == shared["area"]

    # Rewriting the list later keeps when each was first said.
    monkeypatch.setattr("minebot.app.life_profile._now", lambda: "2026-10-29T08:00:00Z")
    record = store.update({"belongings": [chest]}, expected_revision=1, reason="the farm is gone")
    assert record["profile"]["belongings"][0]["said_at"] == "2026-10-28T20:00:00Z"


@pytest.mark.parametrize("item,message", [
    ({"owner": "qwait", "dimension": "overworld", "said_by": "qwait"}, "either pos or area"),
    ({"owner": "qwait", "pos": [1, 2, 3], "area": {"from": [1, 2, 3], "to": [1, 2, 3]}, "dimension": "overworld",
      "said_by": "qwait"}, "either pos or area"),
    ({"owner": "qwait", "pos": [1, 2, 3], "said_by": "qwait"}, r"belongings\[0\].dimension is needed"),
    ({"owner": "qwait", "pos": [1, 2, 3], "dimension": "overworld"}, r"belongings\[0\].said_by"),
    ({"pos": [1, 2, 3], "dimension": "overworld", "said_by": "qwait"}, r"belongings\[0\].owner"),
])
def test_belonging_shapes_are_checked(tmp_path, item, message):
    store = LifeStore(tmp_path, SCOPE)
    with pytest.raises(LifeProfileError, match=message):
        store.update({"belongings": [item]}, expected_revision=0, reason="r")


def test_update_life_declares_the_home_outposts_and_belongings(tmp_path):
    registry = registry_with()
    register_life_tools(registry, LifeStore(tmp_path, SCOPE))
    tool = registry.get("update_life")
    fields = tool.input_schema["properties"]["changes"]["properties"]
    assert {"name", "area", "storage"} <= set(fields["home"]["properties"])
    assert fields["outposts"]["maxItems"] == 8 and fields["belongings"]["maxItems"] == 32
    assert {"owner", "said_by", "said_at"} <= set(fields["belongings"]["items"]["properties"])
    for words in ("storage", "outposts", "belongings", "left out means the overworld"):
        assert words in tool.description
    result = call(registry, tool.name, changes={"outposts": [{"name": "n", "pos": [0, 64, 0], "dimension": "end"}]},
                  expected_revision=0, reason="r")
    assert result.success and result.metrics["profile"]["outposts"][0]["dimension"] == "minecraft:the_end"
