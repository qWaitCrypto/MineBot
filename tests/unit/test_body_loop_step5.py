"""Threat action eligibility, concrete bindings and passive reaction timing."""
import asyncio
import time
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from minebot.app import reflex_question as rq
from minebot.app.body_loop import BodyLoop
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app.execution_snapshot import ExecutionSnapshots


def test_real_lite_combat_progress_keeps_observed_enemy_and_inventory_across_calls():
    recorded = json.loads((Path(__file__).parent / "fixtures/step5-lite-threat.json").read_text())
    owner = SimpleNamespace(decision_client=SimpleNamespace(),
                            trace=SimpleNamespace(emit=lambda *a, **k: None))
    loop = BodyLoop(owner)
    loop._frame(recorded["6026"]["frame_input"]["execution"])
    raw = recorded["6034"]["frame_input"]["execution"]
    assert not {"entities", "inventory", "inventory_counts"} & raw["facts"].keys()
    enriched = loop._frame(raw)
    assert rq.hostiles(enriched["facts"])[0]["id"] == rq.hostiles(
        recorded["6026"]["frame_input"]["execution"]["facts"])[0]["id"]
    assert enriched["facts"]["inventory"]["iron_sword"] == 1
    assert "move_away" in rq.protective_options(enriched["facts"])
    assert "No hostile" not in rq._threat_sentence(enriched["facts"])
    assert enriched["facts"]["health"] == 17
    saved = enriched["_frame_input"]
    assert saved["execution"] == raw
    replica = BodyLoop(owner)
    replica._ambient_facts = copy.deepcopy(saved["ambient_facts"])
    replica._vitals._samples = copy.deepcopy(saved["vital_samples"])
    replica.safe_standing_position = copy.deepcopy(saved["safe_standing_position"])
    with patch("time.monotonic", return_value=saved["monotonic_s"]):
        assert replica._frame(saved["execution"]) == enriched
    cleared = loop._frame({"facts": {"entities": [], "inventory_counts": {}}})["facts"]
    assert not rq.hostiles(cleared) and rq._inventory(cleared) == {}
    assert "inventory" not in cleared and "nearby_entities" not in cleared


def test_call_boundary_does_not_carry_previous_call_phase_progress_or_deltas():
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace()))
    loop._frame({"facts": {**facts(), "phase": "moving", "inventory_delta": {"stone": 3},
                            "navigation": {"pathing": True}, "last_progress": "movement"}})
    observed = loop._frame({"operation_id": "new", "facts": {"health": 19}})["facts"]
    assert not {"phase", "inventory_delta", "navigation", "last_progress"} & observed.keys()
    assert observed["health"] == 19 and rq.hostiles(observed)


def test_snapshot_fresh_empty_entities_clear_all_previous_entity_aliases():
    snapshots = ExecutionSnapshots()
    snapshots.begin("call", "engage_entity")
    snapshots.observe("call", {"nearby_entities": facts()["entities"]})
    assert rq.hostiles(snapshots.current()["facts"])
    snapshots.observe("call", {"entities": []})
    assert not rq.hostiles(snapshots.current()["facts"])


def facts(distance=3, inventory=None):
    return {"health": 20, "food": 20, "oxygen": 300, "selected_item": "iron_sword",
            "position": [0, 64, 0], "inventory": inventory or {},
            "entities": [{"id": "far", "hostile": True, "name": "mob", "dist2": 400, "pos": [20, 64, 0]},
                         {"id": "near", "hostile": True, "name": "mob", "dist2": distance**2, "pos": [distance, 64, 0]}]}


def test_threat_range_uses_one_eligibility_compiler_for_idle_and_work():
    raw = facts()
    idle = rq.idle_options(raw)
    work = rq.frame_options(tool="move_to", params={}, intent="Reach the hill", facts=raw, running=True)
    assert {"engage", "move_away"} <= set(idle) & set(work)
    assert "ranged" not in idle
    far = facts(20)
    assert not {"engage", "ranged", "move_away"} & set(rq.idle_options(far))
    far.update(health=6, health_before=20)
    assert {"engage", "move_away"} <= set(rq.idle_options(far))


def test_melee_and_ranged_bind_the_observed_uuid_not_a_reselected_nearest_target():
    raw = facts(inventory={"minecraft:crossbow": 1, "minecraft:arrow": 4})
    assert "crossbow" in rq.idle_options(raw)["ranged"]
    assert CallSupervisor._default_action("engage", {"facts": raw}) == ("engage_entity", {"target": "near"})
    assert CallSupervisor._default_action("ranged", {"facts": raw}) == ("ranged_attack", {"target": "near", "weapon": "crossbow"})
    raw["inventory"].pop("minecraft:arrow")
    assert "ranged" not in rq.idle_options(raw)
    for entity in raw["entities"]: entity.pop("id")
    assert "engage" not in rq.idle_options(raw)


def test_threat_call_uses_one_normal_question_and_can_switch_to_a_different_target():
    raw = facts()
    record = {"tool": "engage_entity", "params": {"target": "near"},
              "supervision_context": {"is_reflex_action": True, "reflex_action_choice": "engage"}}
    tool = SimpleNamespace(name="engage_entity")
    options = CallSupervisor._options_for(tool, record, execution={"state": "running", "facts": raw})
    assert "next_step" not in options and "engage" not in options
    assert set(rq.frame_questions(options, protecting=True)) == {"q"}
    assert "move_away" in options and "call_agent" in options
    raw["entities"][1]["id"] = "another"
    changed = CallSupervisor._options_for(tool, record, execution={"state": "running", "facts": raw})
    assert "engage" in changed


def test_reaction_timing_reports_first_native_progress_once_and_unknown_is_inert():
    async def run():
        events=[]
        owner=SimpleNamespace(decision_client=SimpleNamespace(),
                              trace=SimpleNamespace(emit=lambda event, **kw: events.append((event,kw))))
        loop=BodyLoop(owner)
        loop._reaction_event_loop=asyncio.get_running_loop()
        loop._observe_reaction_frame(facts())
        await asyncio.sleep(.001)
        chain=loop._reaction_question(facts(),time.monotonic())
        loop._reaction_answer(chain,"unknown")
        assert loop._reaction_selection is None
        loop._reaction_answer(chain,"engage")
        loop._reaction_actions["action"]={**loop._reaction_selection,"tool":"engage_entity"}
        loop.observe_native_action_progress("action", {"action_id":"native","sampled_at_ms":100})
        loop.observe_native_action_progress("action", {"action_id":"native","sampled_at_ms":101})
        await asyncio.sleep(0)
        assert len(events)==1 and events[0][0]=="reflex_reaction_chain"
        assert events[0][1]["target"]=="near" and events[0][1]["first_native_progress"] is True
        assert events[0][1]["event_to_request_s"] > 0
    asyncio.run(run())


def test_supervised_execution_forwards_its_native_observations_to_the_timing_hook():
    from minebot.app.execution_lane import SerialExecutionLane
    from minebot.contract.execution import current_tool_observation
    async def run():
        lane=SerialExecutionLane()
        observed=[]
        owner=SimpleNamespace(decision_client=SimpleNamespace(), execution_lane=lane,
                              direct_call_handoffs={}, trace=SimpleNamespace(emit=lambda *a, **k: None))
        supervisor=CallSupervisor(owner)
        supervisor.observe_native_action_progress=lambda call, payload: observed.append((call, payload))
        supervisor._start_monitor=lambda *a: None
        def action():
            current_tool_observation()[1]({"action_id":"native","sampled_at_ms":123})
            return {"success":True}
        try:
            result=await supervisor.run(SimpleNamespace(name="move_away"),action,(),call_id="response",timeout_s=1)
            assert result["success"] is True
            assert observed==[("response",{"action_id":"native","sampled_at_ms":123})]
        finally:lane.close()
    asyncio.run(run())
