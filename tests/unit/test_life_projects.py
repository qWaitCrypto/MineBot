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
           "work": {"kind": "call", "id": "farm", "tool": "farm_area",
                    "arguments": {"from": [150, 69, 160], "to": [156, 69, 166]}}}


def test_active_projects_are_idle_options_but_not_while_the_agent_thinks():
    profile = {"routine": "Tend my field by day.",
               "projects": [PROJECT, {**PROJECT, "id": "fence", "status": "paused"}]}
    options = rq.idle_options({"position": [0, 64, 0], "life_profile": profile})
    assert "project_wheat-field" in options and "project_fence" not in options
    assert "wheat_seeds 0 in the inventory" in options["project_wheat-field"]
    assert list(options).index("project_wheat-field") < list(options).index("stay")
    thinking = rq.idle_options({"position": [0, 64, 0], "life_profile": profile, "agent_thinking": True})
    assert "project_wheat-field" not in thinking


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
    assert woken == []


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
    assert runtime.life_store.project_results("wheat-field")[-1]["outcome"] == "cancelled"
    # Preempted by the Agent itself: no project fact; the plan's own fact only.
    assert [fact.get("kind") for fact in woken] == [None]


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
        **runtime.life_store.project_results("wheat-field")[-1], "outcome": "cancelled", "reason": "agent_thinking"}
    assert woken == []
