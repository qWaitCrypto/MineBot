"""Per-turn Agent effort from facts the turn has (agent-effort-routing-20261108.md, user 2026-11-09)."""

import dataclasses
from types import SimpleNamespace

import pytest
from agents import Agent, ModelSettings
from openai.types.shared import Reasoning

from minebot.app.effort_routing import choose_effort
from minebot.app.reflex_tools import ReflexManager
from minebot.app.runner import AgentRuntime


def body(hurt_age=None):
    source = None if hurt_age is None else {"damage_type": "minecraft:mob_attack", "seconds_since_hurt": hurt_age}
    return SimpleNamespace(pos=(0.0, 64.0, 0.0), hurt_source=source)


def mob(distance, kind="zombie", **extra):
    return {"type": kind, "hostile": True, "nearest_dist": distance, **extra}


def wake(reason, name=None):
    return {"reason": reason, "detail": {"name": name} if name else {}}


@pytest.mark.parametrize("kwargs, effort, why", [
    (dict(state=body(hurt_age=10)), "low", "hurt 10 s ago"),
    (dict(state=body(), entities=[mob(5, "creeper")], body_busy=True), "low", "creeper at 5 blocks"),
    (dict(state=body(), wake=wake("respawned")), "low", "woken by respawned"),
    (dict(state=body(hurt_age=10), deep_requested=True), "low", "hurt 10 s ago"),
    (dict(state=body(), deep_requested=True), "xhigh", "the Agent asked to think deeply"),
    (dict(state=body(), body_busy=True), "high", "the Body is working and no danger is near"),
    (dict(state=body(), wake=wake("ladder_low:none_started")), "high", "woken by ladder_low and no danger is near"),
    (dict(state=body(), wake=wake("body_event", "dawn")), "high", "woken by dawn and no danger is near"),
    (dict(state=body(), wake=wake("call_result")), "low", "default"),
    (dict(state=body(), entities=[mob(12)], body_busy=True), "low", "default"),
    (dict(state=body(hurt_age=45), body_busy=True), "low", "default"),
    (dict(state=body(), entities=[{"type": "cow", "hostile": False, "nearest_dist": 2}], body_busy=True),
     "high", "the Body is working and no danger is near"),
])
def test_effort_follows_the_facts(kwargs, effort, why):
    choice = choose_effort(**{"wake": None, **kwargs})
    assert (choice.effort, choice.why) == (effort, why)


def runtime_with(*, busy=False, request=None, hurt_age=None):
    runtime = object.__new__(AgentRuntime)
    events = []
    runtime.trace = SimpleNamespace(emit=lambda event, **payload: events.append((event, payload)))
    runtime.agent_context = SimpleNamespace(_last_state=body(hurt_age), _nearby_entities=())
    runtime.reflex_manager = SimpleNamespace(body_busy=lambda: busy)
    runtime.deep_thought_request = request
    return runtime, events


def low_agent():
    return Agent(name="a", model_settings=ModelSettings(reasoning=Reasoning(effort="low"), temperature=0.3))


def test_turn_agent_keeps_its_settings_and_changes_only_effort(monkeypatch):
    monkeypatch.setenv("MINEBOT_AGENT_EFFORT_ROUTING", "1")
    runtime, events = runtime_with(busy=True)
    turned = runtime._agent_with_turn_effort(low_agent(), wake("call_result"))
    assert turned.model_settings.reasoning.effort == "high"
    assert turned.model_settings.temperature == 0.3
    assert events[-1][0] == "agent_effort" and events[-1][1]["effort"] == "high" and events[-1][1]["base"] == "low"


def test_low_is_the_provider_effort_and_routing_off_changes_nothing(monkeypatch):
    monkeypatch.setenv("MINEBOT_AGENT_EFFORT_ROUTING", "1")
    runtime, _ = runtime_with(hurt_age=3, busy=True)
    agent = low_agent()
    assert runtime._agent_with_turn_effort(agent, None) is agent
    monkeypatch.delenv("MINEBOT_AGENT_EFFORT_ROUTING")
    runtime, events = runtime_with(busy=True)
    assert runtime._agent_with_turn_effort(agent, None) is agent and events == []


def test_a_deep_thought_request_is_used_once_and_waits_out_danger(monkeypatch):
    monkeypatch.setenv("MINEBOT_AGENT_EFFORT_ROUTING", "1")
    runtime, _ = runtime_with(request={"about": "why do I keep dying at night"}, hurt_age=5)
    runtime._agent_with_turn_effort(low_agent(), None)
    assert runtime.deep_thought_request is not None
    runtime.agent_context._last_state = body(hurt_age=90)
    turned = runtime._agent_with_turn_effort(low_agent(), wake("deep_thought"))
    assert turned.model_settings.reasoning.effort == "xhigh"
    assert runtime.deep_thought_request is None
    assert runtime._agent_with_turn_effort(low_agent(), None).model_settings.reasoning.effort == "low"


def test_think_deeply_records_the_request_and_wakes_a_turn():
    facts = []
    runtime = SimpleNamespace(notify_reflex_fact=facts.append)
    manager = ReflexManager.__new__(ReflexManager)
    manager.runtime = runtime
    result = manager.request_deep_thought({"about": "plan the Nether trip"})
    assert result["success"] and result["reason"] == "deep_thought_requested"
    assert runtime.deep_thought_request == {"about": "plan the Nether trip"}
    assert facts[0]["kind"] == "deep_thought" and facts[0]["reason"] == "plan the Nether trip"
    assert not manager.request_deep_thought({"about": " "})["success"]


def test_think_deeply_is_a_tool_only_with_routing_on(monkeypatch):
    from minebot.app.reflex_tools import register_reflex_tools
    from minebot.brain.registry import ToolRegistry

    def names():
        registry = ToolRegistry()
        register_reflex_tools(registry, ReflexManager.__new__(ReflexManager))
        return {tool.name for tool in registry.all()} if hasattr(registry, "all") else set(registry.names())

    monkeypatch.delenv("MINEBOT_AGENT_EFFORT_ROUTING", raising=False)
    assert "think_deeply" not in names()
    monkeypatch.setenv("MINEBOT_AGENT_EFFORT_ROUTING", "1")
    assert "think_deeply" in names()
