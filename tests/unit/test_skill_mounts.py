"""Skills mounted on plan-ladder items (agent-skills-v2-20261105.md §2.1)."""

from types import SimpleNamespace

from minebot.app.ladder_view import ladder_section
from minebot.app.life_profile import MAX_MOUNTED_SKILL_CHARS, MAX_MOUNTED_SKILLS, LifeStore, register_life_tools
from minebot.app.runtime_state import RuntimeScope
from minebot.app.skill_mounts import SkillMounts, ladder_mounts, skill_mount_check
from minebot.brain.context import HARNESS_FACT_PREFIX, AgentContext
from minebot.brain.registry import ToolRegistry


class FakeSkills:
    def __init__(self, bodies, unloadable=()):
        self.bodies = dict(bodies)
        self.unloadable = set(unloadable)

    def read(self, name, version=None):
        if name not in self.bodies:
            return None
        missing = ("craft_item",) if name in self.unloadable else ()
        return SimpleNamespace(name=name, body=self.bodies[name], status="active",
                               loadable=not missing, missing_tools=missing)


SKILLS = FakeSkills({f"skill-{index}": f"# Skill {index}\n\nbody {index}" for index in range(6)})


def _life(tmp_path, skills=SKILLS):
    store = LifeStore(tmp_path, RuntimeScope("s", "w", "MountBot"))
    registry = ToolRegistry()
    register_life_tools(registry, store, skill_check=skill_mount_check(skills))
    tool = registry.get("update_life_project")

    def write(project, operation="upsert", before=None):
        return tool.callable({"operation": operation, "project": project, "before": before,
                              "expected_revision": store.read()["revision"], "reason": "test"})

    return store, write, tool


def _item(store, item_id):
    return next(item for item in store.profile().get("projects") or [] if item["id"] == item_id)


def test_update_life_project_mounts_replaces_and_removes_skills(tmp_path):
    store, write, _tool = _life(tmp_path)
    result = write({"id": "end-fight", "intent": "Kill the dragon.", "skills": ["skill-0", "skill-1"]})
    assert result.success, result.metrics
    assert _item(store, "end-fight")["skills"] == ["skill-0", "skill-1"]
    assert result.metrics["mounted_skills"] == ["skill-0", "skill-1"]
    assert "SKILL_MOUNTED" in result.metrics["skills_note"]
    # Upsert without skills keeps them, as with every other field.
    assert write({"id": "end-fight", "intent": "Kill the dragon today."}).success
    assert _item(store, "end-fight")["skills"] == ["skill-0", "skill-1"]
    assert write({"id": "end-fight", "skills": ["skill-2"]}).success
    assert _item(store, "end-fight")["skills"] == ["skill-2"]
    assert write({"id": "end-fight", "skills": []}).success
    assert "skills" not in _item(store, "end-fight")


def test_an_unknown_or_unloadable_skill_is_a_typed_rejection(tmp_path):
    skills = FakeSkills({"skill-0": "body"}, unloadable={"skill-0"})
    store, write, _tool = _life(tmp_path, skills)
    missing = write({"id": "a", "intent": "A.", "skills": ["no-such-skill"]})
    assert not missing.success and missing.reason == "skill_not_found"
    assert "no-such-skill" in missing.metrics["error"]
    unloadable = write({"id": "a", "intent": "A.", "skills": ["skill-0"]})
    assert not unloadable.success and unloadable.reason == "skill_dependency_unavailable"
    malformed = write({"id": "a", "intent": "A.", "skills": ["Not A Name"]})
    assert not malformed.success and malformed.reason == "life_project_rejected"
    assert store.profile().get("projects") is None


def test_the_write_that_would_mount_a_fifth_skill_is_rejected_naming_the_cap(tmp_path):
    store, write, _tool = _life(tmp_path)
    five = write({"id": "a", "intent": "A.", "skills": [f"skill-{index}" for index in range(5)]})
    assert not five.success and five.reason == "skill_mount_count_exceeded"
    for index in range(MAX_MOUNTED_SKILLS):
        assert write({"id": f"item-{index}", "intent": "I.", "skills": [f"skill-{index}"]}).success
    fifth = write({"id": "item-4", "intent": "I.", "skills": ["skill-4"]})
    assert not fifth.success and fifth.reason == "skill_mount_count_exceeded"
    assert f"at most {MAX_MOUNTED_SKILLS}" in fifth.metrics["error"]
    assert "item-4" not in [item["id"] for item in store.profile()["projects"]]
    # A Skill another item already holds adds nothing to the mounted set.
    assert write({"id": "item-4", "intent": "I.", "skills": ["skill-0"]}).success


def test_the_write_that_would_pass_the_character_cap_is_rejected(tmp_path):
    big = FakeSkills({name: "x" * 15_000 for name in ("big-a", "big-b", "big-c")})
    store, write, _tool = _life(tmp_path, big)
    assert write({"id": "a", "intent": "A.", "skills": ["big-a", "big-b"]}).success
    over = write({"id": "b", "intent": "B.", "skills": ["big-c"]})
    assert not over.success and over.reason == "skill_mount_chars_exceeded"
    assert str(MAX_MOUNTED_SKILL_CHARS) in over.metrics["error"]
    # Freeing room by replacing a Skill is allowed.
    assert write({"id": "a", "skills": ["big-a"]}).success
    assert write({"id": "b", "intent": "B.", "skills": ["big-c"]}).success


def test_tool_schema_and_description_say_what_mounting_does(tmp_path):
    _store, _write, tool = _life(tmp_path)
    schema = tool.input_schema["properties"]["project"]["properties"]["skills"]
    assert schema["maxItems"] == MAX_MOUNTED_SKILLS
    assert "skills mounts Skills on the item" in tool.description


def test_mount_and_unmount_append_messages_and_traces_in_first_mount_order(tmp_path):
    store, write, _tool = _life(tmp_path)
    delivered, events = [], []
    mounts = SkillMounts(SKILLS, deliver=delivered.append, emit=lambda event, **fields: events.append((event, fields)))

    def sync():
        mounts.sync(ladder_mounts(store.profile().get("projects") or [], store.project_state))

    write({"id": "later", "intent": "Later.", "skills": ["skill-1"]})
    sync()
    # "first" goes ahead of "later" on the ladder; the mounted order stays
    # first-mount order, and skill-1 keeps the item it was first mounted for.
    assert write({"id": "first", "intent": "First.", "skills": ["skill-0", "skill-1"]}, before="later").success
    assert [item["id"] for item in store.profile()["projects"]] == ["first", "later"]
    sync()
    assert delivered == [
        "SKILL_MOUNTED name=skill-1 for=later\n# Skill 1\n\nbody 1",
        "SKILL_MOUNTED name=skill-0 for=first\n# Skill 0\n\nbody 0",
    ]
    assert [mount.name for mount in mounts.mounted()] == ["skill-1", "skill-0"]
    assert events[0] == ("skill_mounted", {"name": "skill-1", "item_id": "later",
                                           "chars": len("# Skill 1\n\nbody 1"), "announced": True})
    assert mounts.budget() == {"mounted_skill_count": 2,
                               "mounted_skill_chars": 2 * len("# Skill 0\n\nbody 0")}
    # "later" leaves: skill-1 is still held by "first", nothing changes.
    write({"id": "later"}, operation="remove")
    sync()
    assert len(delivered) == 2
    # Marking "first" done takes it off the ladder: both unmount.
    write({"id": "first", "status": "done"})
    sync()
    assert delivered[2:] == ["SKILL_UNMOUNTED name=skill-1: no longer in effect",
                             "SKILL_UNMOUNTED name=skill-0: no longer in effect"]
    assert [name for name, _fields in events].count("skill_unmounted") == 2
    assert mounts.mounted() == []


def test_a_finished_item_still_in_projects_no_longer_mounts():
    projects = [{"id": "done-one", "intent": "D.", "status": "done", "skills": ["skill-0"]},
                {"id": "live", "intent": "L.", "status": "paused", "skills": ["skill-1"]}]
    assert ladder_mounts(projects, lambda _item: {}) == [("skill-1", "live")]


def test_a_restart_seeds_the_mounted_set_without_messages():
    delivered = []
    mounts = SkillMounts(SKILLS, deliver=delivered.append)
    mounts.sync([("skill-0", "a")], announce=False)
    assert delivered == [] and mounts.mounted_texts() == ["SKILL_MOUNTED name=skill-0 for=a\n# Skill 0\n\nbody 0"]
    mounts.sync([("skill-0", "a")])
    assert delivered == []


def test_a_mount_message_is_its_own_conversation_item_in_queue_order():
    ctx = AgentContext(system_prompt="sys", goal_text="live")
    ctx.queue_harness_fact("REFLEX_RESULT: done")
    ctx.queue_conversation_item("SKILL_MOUNTED name=skill-0 for=a\n# Skill 0\n\nline one\n  line two")
    ctx.observe_user_message("hello")
    items, count = ctx.pending_turn_items(fallback="continue")
    assert count == 3
    assert items == [
        {"role": "user", "content": f"{HARNESS_FACT_PREFIX}REFLEX_RESULT: done"},
        {"role": "user", "content": "SKILL_MOUNTED name=skill-0 for=a\n# Skill 0\n\nline one\n  line two"},
        {"role": "user", "content": "hello"},
    ]
    assert ctx.pending_item_texts() == [items[1]["content"]]
    ctx.acknowledge_turn_input(count)
    assert ctx.pending_turn_items(fallback="continue") == ("continue", 0)
    assert ctx.pending_item_texts() == []
    # Without a queued item the input is the same single text as before.
    ctx.queue_harness_fact("a fact")
    assert ctx.pending_turn_items(fallback="x") == ctx.pending_turn_input(fallback="x")


def test_ladder_line_shows_the_items_skills():
    record = {"profile": {"projects": [{"id": "end-fight", "intent": "Kill the dragon.", "status": "active",
                                        "skills": ["end-crystals", "end-dragon-beds"]}]}}
    text = ladder_section(record, lambda _item: {})
    assert "end-fight [think] Kill the dragon. skills: end-crystals, end-dragon-beds" in text


# -- skills on a delegate_work plan (dragon-unit-tests-20261030.md §6.y.22 item 2) --


import asyncio

import pytest

from minebot.app.body_control import BodyControl
from minebot.app.life_profile import project_plan
from minebot.app.reflex_tools import ReflexManager
from minebot.app.skill_mounts import merged_mounts
from minebot.app.work_runtime import WorkPlanError, validate_plan


class _Decision:
    async def ask(self, payload, *, priority=None):
        return SimpleNamespace(choice="step_done", raw={"choice": "step_done"})


class _Trace:
    def __init__(self):
        self.events = []

    def emit(self, event, **payload):
        self.events.append((event, payload))


def _plan_runtime(release, life_store=None):
    calls = []
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
    runtime = SimpleNamespace(
        body_control=BodyControl(), decision_client=_Decision(), trace=_Trace(),
        responses_kernel=None, last_known_body_state={}, registry=registry, weld_context=None,
        notify_reflex_fact=lambda fact: None)
    if life_store is not None:
        runtime.life_store = life_store

    async def run_tool(_tool, _callback, _tool_arg, arguments, *_args, **_kwargs):
        calls.append(arguments["x"])
        await release[arguments["x"]].wait()
        return {"success": True, "reason": "done", "metrics": {}}

    async def stop(reason, cancel_reflex=False):
        for x in list(calls):  # the call running now ends
            release[x].set()
        return {"settled": True, "native_stopped": True}

    runtime.run_tool = run_tool
    runtime.cancel_active_execution_with_facts = stop
    return runtime, calls


def _skill_plan(x, skills, goal="dig the stairs"):
    return {"language": "minebot-work/0.2", "goal": goal, "timeout_s": 4, "skills": skills,
            "work": {"kind": "call", "intent": f"step {x} done", "id": f"n{x}", "tool": "fake",
                     "arguments": {"x": x}}}


def _plan_mounts(runtime, skills=SKILLS, ladder=()):
    """The composition in phase1_runtime: a manager, a tracker, the turn-start sync."""
    manager = ReflexManager(runtime)
    manager.skill_check = skill_mount_check(skills)
    manager.ladder_mounts = lambda: list(ladder)
    delivered, events = [], []
    mounts = SkillMounts(skills, deliver=delivered.append,
                         emit=lambda event, **fields: events.append((event, fields)))

    def sync():
        mounts.sync(merged_mounts(manager.ladder_mounts(), manager.plan_skill_mounts()))

    return manager, mounts, delivered, events, sync


async def _until(predicate, steps=200):
    for _ in range(steps):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def test_a_plans_skills_mount_while_it_runs_and_unmount_when_it_completes():
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, calls = _plan_runtime(release)
        manager, mounts, delivered, events, sync = _plan_mounts(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        result = manager.delegate({"plan": _skill_plan(1, ["skill-0", "skill-1"])})
        assert result["success"], result
        assert result["metrics"]["plan_skills"] == ["skill-0", "skill-1"]
        assert "SKILL_MOUNTED" in result["metrics"]["skills_note"]
        activity_id = result["metrics"]["activity_id"]
        await _until(lambda: calls == [1])
        sync()
        assert delivered == [f"SKILL_MOUNTED name=skill-0 for=plan:{activity_id}\n# Skill 0\n\nbody 0",
                             f"SKILL_MOUNTED name=skill-1 for=plan:{activity_id}\n# Skill 1\n\nbody 1"]
        assert events[0] == ("skill_mounted", {"name": "skill-0", "item_id": f"plan:{activity_id}",
                                               "chars": len("# Skill 0\n\nbody 0"), "announced": True,
                                               "holder": "plan", "plan_id": activity_id})
        sync()
        assert len(delivered) == 2  # nothing changes while it runs
        release[1].set()
        assert await _until(lambda: manager.ended_activity(activity_id) is not None)
        sync()
        assert delivered[2:] == ["SKILL_UNMOUNTED name=skill-0: no longer in effect",
                                 "SKILL_UNMOUNTED name=skill-1: no longer in effect"]
        assert events[-1][0] == "skill_unmounted" and events[-1][1]["plan_id"] == activity_id
        assert mounts.mounted() == []
        await manager.close()

    asyncio.run(scenario())


def test_a_cancelled_plan_unmounts_its_skills():
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, calls = _plan_runtime(release)
        manager, mounts, delivered, _events, sync = _plan_mounts(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        activity_id = manager.delegate({"plan": _skill_plan(1, ["skill-2"])})["metrics"]["activity_id"]
        await _until(lambda: calls == [1])
        sync()
        assert [mount.name for mount in mounts.mounted()] == ["skill-2"]
        manager.control({"operation": "cancel", "activity_id": activity_id})
        assert await _until(lambda: manager.ended_activity(activity_id) is not None)
        assert manager.ended_activity(activity_id)["status"] == "cancelled"
        sync()
        assert delivered[-1] == "SKILL_UNMOUNTED name=skill-2: no longer in effect"
        await manager.close()

    asyncio.run(scenario())


def test_take_control_ends_the_plan_and_its_mounts():
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, calls = _plan_runtime(release)
        manager, mounts, _delivered, _events, sync = _plan_mounts(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        activity_id = manager.delegate({"plan": _skill_plan(1, ["skill-2"])})["metrics"]["activity_id"]
        await _until(lambda: calls == [1])
        sync()
        manager.control({"operation": "take_control"})
        assert await _until(lambda: manager.ended_activity(activity_id) is not None)
        sync()
        assert mounts.mounted() == []
        await manager.close()

    asyncio.run(scenario())


def test_a_skill_a_ladder_item_also_holds_stays_mounted_after_the_plan_ends():
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, calls = _plan_runtime(release)
        manager, mounts, delivered, _events, sync = _plan_mounts(runtime, ladder=[("skill-0", "end-fight")])
        manager.bind_loop(asyncio.get_running_loop())
        activity_id = manager.delegate({"plan": _skill_plan(1, ["skill-0", "skill-1"])})["metrics"]["activity_id"]
        await _until(lambda: calls == [1])
        sync()
        # The ladder item holds skill-0 first; the plan adds only skill-1.
        assert [(mount.name, mount.item_id) for mount in mounts.mounted()] == [
            ("skill-0", "end-fight"), ("skill-1", f"plan:{activity_id}")]
        release[1].set()
        assert await _until(lambda: manager.ended_activity(activity_id) is not None)
        sync()
        assert [mount.name for mount in mounts.mounted()] == ["skill-0"]
        assert delivered[-1] == "SKILL_UNMOUNTED name=skill-1: no longer in effect"
        await manager.close()

    asyncio.run(scenario())


def test_two_running_plans_hold_a_shared_skill_until_both_end():
    manager = ReflexManager(SimpleNamespace(trace=_Trace()))
    first = SimpleNamespace(activity_id="a", status="running", plan={"skills": ["skill-0", "skill-1"]})
    second = SimpleNamespace(activity_id="b", status="waiting", plan={"skills": ["skill-0"]})
    manager._activities = {"a": first, "b": second}
    assert merged_mounts(manager.plan_skill_mounts()) == [("skill-0", "plan:a"), ("skill-1", "plan:a")]
    first.status = "completed"
    assert merged_mounts(manager.plan_skill_mounts()) == [("skill-0", "plan:b")]
    second.status = "cancelled"
    assert manager.plan_skill_mounts() == []


def test_updating_a_plan_replaces_its_skills():
    async def scenario():
        release = {1: asyncio.Event(), 2: asyncio.Event()}
        runtime, calls = _plan_runtime(release)
        manager, mounts, delivered, _events, sync = _plan_mounts(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        activity_id = manager.delegate({"plan": _skill_plan(1, ["skill-0"])})["metrics"]["activity_id"]
        await _until(lambda: calls == [1])
        sync()
        updated = manager.control({"operation": "update", "activity_id": activity_id,
                                   "spec": _skill_plan(2, ["skill-3"])})
        assert updated["success"], updated
        assert await _until(lambda: 2 in calls)
        sync()
        assert [mount.name for mount in mounts.mounted()] == ["skill-3"]
        assert "SKILL_UNMOUNTED name=skill-0: no longer in effect" in delivered
        release[2].set()
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("skills,ladder,reason", [
    (["no-such-skill"], (), "skill_not_found"),
    (["skill-5"], (), "skill_dependency_unavailable"),
    ([f"skill-{index}" for index in range(5)], (), "skill_mount_count_exceeded"),
    (["skill-3", "skill-4"], [("skill-0", "a"), ("skill-1", "a"), ("skill-2", "b")], "skill_mount_count_exceeded"),
    (["Not A Name"], (), "invalid_plan"),
])
def test_a_plan_whose_skills_cannot_mount_is_refused_typed_and_never_starts(skills, ladder, reason):
    async def scenario():
        release = {1: asyncio.Event()}
        runtime, calls = _plan_runtime(release)
        catalog = FakeSkills({f"skill-{index}": f"body {index}" for index in range(6)}, unloadable={"skill-5"})
        manager, _mounts, _delivered, _events, _sync = _plan_mounts(runtime, catalog, ladder=ladder)
        manager.bind_loop(asyncio.get_running_loop())
        result = manager.delegate({"plan": _skill_plan(1, skills)})
        await asyncio.sleep(0.05)
        await manager.close()
        return result, calls, manager

    result, calls, manager = asyncio.run(scenario())
    assert result["success"] is False and result["reason"] == reason, result
    assert calls == [] and manager._activities == {}


def test_a_plan_over_the_character_cap_is_refused():
    async def scenario():
        runtime, calls = _plan_runtime({1: asyncio.Event()})
        big = FakeSkills({name: "x" * 15_000 for name in ("big-a", "big-b", "big-c")})
        manager, *_rest = _plan_mounts(runtime, big, ladder=[("big-a", "a"), ("big-b", "a")])
        manager.bind_loop(asyncio.get_running_loop())
        return manager.delegate({"plan": _skill_plan(1, ["big-c"])}), calls

    result, calls = asyncio.run(scenario())
    assert result["reason"] == "skill_mount_chars_exceeded" and calls == []
    assert str(MAX_MOUNTED_SKILL_CHARS) in result["metrics"]["error"]


def test_without_a_skill_catalog_a_plan_naming_skills_is_refused():
    runtime, _calls = _plan_runtime({1: asyncio.Event()})
    result = ReflexManager(runtime).delegate({"plan": _skill_plan(1, ["skill-0"])})
    assert result["reason"] == "skill_not_found"


def test_plan_skills_in_the_work_language():
    validate_plan(_skill_plan(1, ["skill-0", "skill-1"]))
    validate_plan({key: value for key, value in _skill_plan(1, []).items()})
    for bad in ("skill-0", [1], ["Bad Name"], ["skill-0", "skill-0"]):
        with pytest.raises(WorkPlanError):
            validate_plan(_skill_plan(1, bad))
    with pytest.raises(WorkPlanError):
        validate_plan(_skill_plan(1, [f"skill-{index}" for index in range(5)]))


def test_a_plan_that_waits_on_the_ladder_keeps_its_skills_for_when_it_runs(tmp_path):
    async def scenario():
        store = LifeStore(tmp_path, RuntimeScope("s", "w", "MountBot"))
        release = {1: asyncio.Event(), 2: asyncio.Event()}
        runtime, calls = _plan_runtime(release, life_store=store)
        manager, mounts, _delivered, _events, sync = _plan_mounts(runtime)
        manager.bind_loop(asyncio.get_running_loop())
        manager.delegate({"plan": _skill_plan(1, ["skill-0"])})
        await _until(lambda: calls == [1])
        queued = manager.delegate({"plan": _skill_plan(2, ["skill-1"], goal="build the room"), "id": "room"})
        assert queued["reason"] == "on_ladder_after_running_plan" and queued["metrics"]["plan_skills"] == ["skill-1"]
        room = _item(store, "room")
        # Not a ladder mount: it waits; its plan carries the Skills when it runs.
        assert "skills" not in room and room["plan_skills"] == ["skill-1"]
        assert project_plan(room)["skills"] == ["skill-1"]
        sync()
        assert [mount.name for mount in mounts.mounted()] == ["skill-0"]
        release[1].set()
        await manager.close()

    asyncio.run(scenario())


def test_delegate_work_description_says_a_plan_may_carry_skills():
    from minebot.app.reflex_tools import register_reflex_tools

    registry = ToolRegistry()
    register_reflex_tools(registry, ReflexManager(SimpleNamespace(trace=_Trace())))
    description = registry.get("delegate_work").description
    assert "A plan may carry skills" in description and "while the plan runs" in description
