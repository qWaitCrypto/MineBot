"""G5: the Agent's own projects, walked by the reflex layer as idle life."""
import asyncio
from types import SimpleNamespace

from minebot.app import reflex_question as rq
from minebot.app.body_control import BodyControl
from minebot.app.life_profile import LifeStore
from minebot.app.reflex_tools import ReflexManager
from minebot.app.runtime_state import RuntimeScope

PROJECT = {"id": "wheat-field", "intent": "wheat_seeds 0 in the inventory", "status": "active",
           "area": "wheat field",
           "work": {"kind": "call", "id": "farm", "tool": "farm_area", "intent": "wheat_seeds 0 in the inventory",
                    "arguments": {"from": [150, 69, 160], "to": [156, 69, 166]}}}


def test_active_projects_are_idle_options_also_while_the_agent_thinks():
    profile = {"routine": "Tend my field by day.",
               "projects": [PROJECT, {**PROJECT, "id": "fence", "status": "paused"}]}
    options = rq.idle_options({"position": [0, 64, 0], "life_profile": profile})
    assert "project_wheat-field" in options and "project_fence" not in options
    assert "wheat_seeds 0 in the inventory" in options["project_wheat-field"]
    assert list(options).index("project_wheat-field") < list(options).index("stay")
    # The ladder runs while the Agent thinks (plan-ladder-20261028.md §7.8).
    thinking = rq.idle_options({"position": [0, 64, 0], "life_profile": profile, "agent_thinking": True})
    assert "project_wheat-field" in thinking and "call_agent" not in thinking


def test_ladder_items_are_offered_by_the_four_conditions_in_the_agents_order():
    walk = {"kind": "call", "tool": "fake", "intent": "x"}
    profile = {"projects": [
        {"id": "smelt", "intent": "Smelt 24 raw_iron.", "status": "active", "work": walk,
         "when": "it is daytime and coal is in the inventory."},
        {"id": "mine-iron", "intent": "Collect 24 raw_iron", "status": "active", "work": walk},
        {"id": "portal", "intent": "Build a nether portal.", "status": "active", "when": "it is day"},
        {"id": "hunt", "intent": "Hunt cows.", "status": "active", "work": walk},
        {"id": "farm", "intent": "Farm.", "status": "active", "work": walk},
    ]}
    states = {"hunt": "set_aside", "farm": "running", "smelt": "waiting"}
    options = rq.ladder_options({"ladder_states": states}, profile)
    assert options == {
        "project_smelt": "Leave what I am doing and work on my project “smelt”: Smelt 24 raw_iron. "
                         "It fits when it is daytime and coal is in the inventory.",
        "project_mine-iron": "Leave what I am doing and work on my project “mine-iron”: Collect 24 raw_iron",
    }
    assert rq.ladder_options({"ladder_states": states, "ladder_held": True}, profile) == {}


def test_an_item_whose_last_run_changed_nothing_is_not_offered_while_the_body_is_as_it_was():
    # T7 2026-10-29: a standing wood item completed with nothing changed 98
    # times in four minutes (s7-later-20261029.md §8.14).
    end = {"outcome": "completed", "start_position": [24.3, 48.0, 1.4], "position": [24.6, 48.0, 1.2],
           "inventory_counts": {"jungle_log": 6, "oak_log": 3}, "inventory_change": {}}
    here = {"position": [24.5, 48.0, 1.9], "inventory": {"oak_log": 3, "jungle_log": 6, "air": 0}}
    assert rq.same_item_run_again(end, here)
    # The Body moved, or its inventory changed: a new attempt.
    assert not rq.same_item_run_again(end, {**here, "position": [26.0, 48.0, 1.4]})
    assert not rq.same_item_run_again(end, {**here, "inventory": {"jungle_log": 7, "oak_log": 3}})
    # The run itself changed something, or did not end by completing.
    assert not rq.same_item_run_again({**end, "inventory_change": {"jungle_log": 1}}, here)
    assert not rq.same_item_run_again({**end, "start_position": [20.0, 48.0, 1.0]}, here)
    assert not rq.same_item_run_again({**end, "outcome": "yielded"}, here)
    # Missing evidence is never "the same".
    assert not rq.same_item_run_again({key: v for key, v in end.items() if key != "inventory_change"}, here)
    assert not rq.same_item_run_again(end, {"position": here["position"]})
    walk = {"kind": "call", "tool": "fake", "intent": "x"}
    profile = {"projects": [{"id": "wood", "intent": "Keep 8 logs.", "status": "active", "work": walk},
                            {"id": "coal", "intent": "Mine coal.", "status": "active", "work": walk}]}
    assert list(rq.ladder_options({"ladder_same_run": ["wood"]}, profile)) == ["project_coal"]


def test_project_frame_adds_life_facts_and_putting_it_down():
    facts = {"time": 12500, "position": [0, 64, 0], "life_intent": "Home before dark.",
             "nearby_entities": [{"type": "minecraft:player", "name": "qwait", "pos": [3, 64, 4]}]}
    state, options = rq.project_frame({"task": "my project"}, {"keep_going": "k", "call_agent": "c"}, facts)
    assert list(options) == ["keep_going", "put_down", "call_agent"]
    assert state["daylight"] == "It is dusk; night falls soon."
    assert state["players"] == "Player qwait is 5 blocks away."
    assert state["life_intent"] == "This is the idle-life intention: “Home before dark.”."


class Trace:
    def __init__(self):
        self.events = []

    def emit(self, event, **payload):
        self.events.append((event, payload))


class Decision:
    async def ask(self, payload, *, priority=None):
        return SimpleNamespace(choice="step_done", raw={"choice": "step_done"})


def manager_with(tmp_path, run_tool):
    trace, woken, holders = Trace(), [], []
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(sidecar=SimpleNamespace(timeout_s=None)))
    runtime = SimpleNamespace(
        body_control=BodyControl(), decision_client=Decision(), trace=trace, responses_kernel=None,
        last_known_body_state={}, registry=registry, weld_context=None, notify_reflex_fact=woken.append,
        life_store=LifeStore(tmp_path, RuntimeScope("s", "w", "LifeBot")),
    )

    async def wrapped(tool, callback, *args, **kwargs):
        holders.append((kwargs.get("claim_holder"), kwargs.get("supervision_context", {}).get("is_life_project")))
        return await run_tool(tool, callback, *args, **kwargs)

    runtime.run_tool = wrapped
    return ReflexManager(runtime), runtime, woken, holders


def test_a_finished_project_is_recorded_and_wakes_the_agent(tmp_path):
    async def scenario():
        async def run_tool(tool, callback, *args, **kwargs):
            return {"success": True, "reason": "done", "metrics": {}}

        manager, runtime, woken, holders = manager_with(tmp_path, run_tool)
        manager.bind_loop(asyncio.get_running_loop())
        assert manager.start_project(PROJECT) is not None
        for _ in range(50):
            await asyncio.sleep(0.01)
            if woken:
                break
        await manager.close()
        return runtime, woken, holders

    runtime, woken, holders = asyncio.run(scenario())
    assert holders == [("life", True)]
    assert woken[0]["kind"] == "life_project" and woken[0]["status"] == "completed"
    assert runtime.life_store.project_results("wheat-field")[-1]["outcome"] == "completed"
    assert runtime.body_control.snapshot().holder == "none"


def test_a_walking_ladder_item_keeps_the_body_busy(tmp_path):
    # A resting Agent's heartbeat waits while the Body walks a ladder item:
    # the reflex layer has it, and its end is a notice (s7-later-20261029.md §8 D2).
    async def scenario():
        release = asyncio.Event()

        async def run_tool(tool, callback, *args, **kwargs):
            await release.wait()
            return {"success": True, "reason": "done", "metrics": {}}

        manager, runtime, woken, _ = manager_with(tmp_path, run_tool)
        manager.bind_loop(asyncio.get_running_loop())
        idle_before = manager.body_busy()
        manager.start_project(PROJECT)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if manager.body_busy():
                break
        walking = manager.body_busy()
        release.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if woken:
                break
        idle_after = manager.body_busy()
        await manager.close()
        return idle_before, walking, idle_after

    idle_before, walking, idle_after = asyncio.run(scenario())
    assert idle_before is False and idle_after is False
    assert walking is True  # not the Agent's own plan, but the Body is busy


def test_a_project_put_down_is_recorded_without_waking_the_agent(tmp_path):
    async def scenario():
        manager = None

        async def run_tool(tool, callback, *args, **kwargs):
            # Decision picked "put the project down" in the running frame.
            manager.put_down_project()
            return {"success": False, "reason": "stopped_by_decision", "metrics": {
                "direct_call_handoff": {"facts": {"reason": "decision_put_down",
                                                  "decision": {"choice": "call_agent"}}}}}

        manager, runtime, woken, _ = manager_with(tmp_path, run_tool)
        manager.bind_loop(asyncio.get_running_loop())
        manager.start_project(PROJECT)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if runtime.life_store.project_results("wheat-field"):
                break
        await manager.close()
        return runtime, woken

    runtime, woken = asyncio.run(scenario())
    assert runtime.life_store.project_results("wheat-field")[-1]["outcome"] == "put_down"
    # A notice the Agent reads on its next turn, not a wake (plan-ladder §7.4).
    assert [(fact["status"], fact["wake"]) for fact in woken] == [("put_down", False)]


def test_the_agents_plan_takes_the_body_from_a_running_project(tmp_path):
    async def scenario():
        release = asyncio.Event()
        stops = []

        async def run_tool(tool, callback, *args, **kwargs):
            if kwargs.get("claim_holder") == "life":
                await release.wait()
            return {"success": True, "reason": "done", "metrics": {}}

        manager, runtime, woken, holders = manager_with(tmp_path, run_tool)

        async def cancel(reason, **kwargs):
            stops.append(reason)
            runtime.body_control.settle()
            return {"settled": True}

        runtime.cancel_active_execution_with_facts = cancel
        manager.bind_loop(asyncio.get_running_loop())
        manager.start_project(PROJECT)
        await asyncio.sleep(0.05)
        assert manager.life_project_running() == "wheat-field"
        plan = {"language": "minebot-work/0.2", "goal": "x", "timeout_s": 4,
                "work": {"kind": "call", "intent": "stone 1 in the inventory", "id": "one", "tool": "fake",
                         "arguments": {}}}
        accepted = manager.delegate({"plan": plan})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if accepted["success"] and manager.read(accepted["metrics"]["activity_id"])["metrics"]["status"] == "completed":
                break
        status = manager.read(accepted["metrics"]["activity_id"])["metrics"]["status"]
        await manager.close()
        return runtime, stops, status, holders, woken

    runtime, stops, status, holders, woken = asyncio.run(scenario())
    assert stops == ["agent_plan_arrived"]
    assert status == "completed" and holders[-1] == ("agent_plan", None)
    # It gave the Body to the Agent: it waits on the ladder, not set aside.
    assert runtime.life_store.project_results("wheat-field")[-1]["outcome"] == "yielded"
    # Preempted by the Agent itself: no project fact; the plan's own fact only.
    assert [fact.get("kind") for fact in woken] == [None]


def test_idle_life_starts_a_project_before_the_agent_has_ever_spoken(tmp_path):
    # life-stage c2: Decision chose the project 237 times, but the manager had
    # no loop yet because no Agent turn had run, and nothing ever started.
    from minebot.app.body_loop import BodyLoop

    async def scenario():
        async def run_tool(tool, callback, *args, **kwargs):
            return {"success": True, "reason": "done", "metrics": {}}

        manager, runtime, woken, holders = manager_with(tmp_path, run_tool)
        runtime.reflex_manager = manager
        loop = BodyLoop.__new__(BodyLoop)
        loop.runtime = runtime
        loop._start_project("wheat-field", {"life_profile": {"projects": [PROJECT]}})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if woken:
                break
        await manager.close()
        return holders, woken

    holders, woken = asyncio.run(scenario())
    assert holders == [("life", True)]
    assert woken and woken[0]["status"] == "completed"


def test_a_running_project_stops_when_the_agent_starts_thinking(tmp_path):
    async def scenario():
        release = asyncio.Event()
        stops = []

        async def run_tool(tool, callback, *args, **kwargs):
            await release.wait()
            return {"success": True, "reason": "done", "metrics": {}}

        manager, runtime, woken, _ = manager_with(tmp_path, run_tool)

        async def cancel(reason, **kwargs):
            stops.append(reason)
            runtime.body_control.settle()
            return {"settled": True}

        runtime.cancel_active_execution_with_facts = cancel
        manager.bind_loop(asyncio.get_running_loop())
        manager.start_project(PROJECT)
        await asyncio.sleep(0.05)
        assert manager.yield_life("agent_thinking") is True
        await asyncio.sleep(0.05)
        await manager.close()
        return runtime, stops, woken

    runtime, stops, woken = asyncio.run(scenario())
    assert stops == ["agent_thinking"]
    assert runtime.life_store.project_results("wheat-field")[-1] == {
        **runtime.life_store.project_results("wheat-field")[-1], "outcome": "yielded", "reason": "agent_thinking"}
    assert woken == []
