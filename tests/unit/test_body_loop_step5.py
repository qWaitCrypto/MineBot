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
    far = facts(40, inventory={"bow": 1, "arrow": 4})
    far["entities"][0]["dist2"] = 60**2
    idle = rq.idle_options(far)
    work = rq.frame_options(tool="move_to", params={}, intent="Reach the hill", facts=far, running=True)
    assert "ranged" in set(idle) & set(work)
    # 40 blocks off: nothing to run from, and no mob at hand to fight (run-16 04:37:41, §13).
    assert not {"move_away", "engage"} & (set(idle) | set(work))
    assert "(40 blocks away)" in idle["ranged"]
    assert work["ranged"] == "Stop the current action and " + idle["ranged"][0].lower() + idle["ranged"][1:]


def test_melee_and_ranged_bind_the_observed_uuid_not_a_reselected_nearest_target():
    raw = facts(inventory={"minecraft:crossbow": 1, "minecraft:arrow": 4})
    assert "crossbow" in rq.idle_options(raw)["ranged"]
    assert CallSupervisor._default_action("engage", {"facts": raw}) == (
        "engage_entity", {"target": "near", "disengage_health": 0})
    assert CallSupervisor._default_action("ranged", {"facts": raw}) == ("ranged_attack", {"target": "near", "weapon": "crossbow", "max_range": 64})
    raw["inventory"].pop("minecraft:arrow")
    assert "ranged" not in rq.idle_options(raw)
    for entity in raw["entities"]: entity.pop("id")
    assert "engage" not in rq.idle_options(raw)


def test_ranged_option_says_arrows_do_not_hurt_an_enderman_and_stays_offered():
    raw = facts(inventory={"minecraft:bow": 1, "minecraft:arrow": 4})
    raw["entities"][1].update(name="Enderman", type="minecraft:enderman")
    text = rq.idle_options(raw)["ranged"]
    assert text.startswith("Shoot the Enderman (3 blocks away) with the bow from the inventory.")
    assert "arrows do not hurt it" in text
    raw["entities"][1].update(name="Zombie", type="minecraft:zombie")
    assert rq.idle_options(raw)["ranged"] == (
        "Shoot the Zombie (3 blocks away) with the bow from the inventory. Arrows left: 4.")


def test_ranged_option_says_the_arrows_already_shot_at_this_target():
    record: dict = {}
    shot = {"target_id": "d1", "target_type": "ender_dragon", "outcome": "in_flight", "age_s": 0.2,
            "health_before": 200.0}
    rq.update_shot_record({"last_shot": shot}, record, now=10.0)
    # The same arrow read again, resolved: still one shot, now one hit.
    rq.update_shot_record({"last_shot": {**shot, "outcome": "hit", "age_s": 1.2}}, record, now=11.0)
    rq.update_shot_record({"last_shot": {**shot, "outcome": "hit", "age_s": 2.2}}, record, now=12.0)
    for n in range(2):
        out = rq.update_shot_record({"last_shot": {**shot, "outcome": "missed", "age_s": 0.1}}, record, now=20.0 + n * 3)
    assert out == [{"target_id": "d1", "target_type": "ender_dragon", "shots": 3, "hits": 1,
                    "health_at_first_shot": 200.0}]
    raw = facts(inventory={"minecraft:bow": 1, "minecraft:arrow": 40})
    raw["entities"][1].update(id="d1", name="Ender Dragon", type="minecraft:ender_dragon", boss=True,
                              health=200.0, max_health=200.0)
    raw["shot_record"] = out
    text = rq.idle_options(raw)["ranged"]
    assert text.endswith("Arrows already shot at this Ender Dragon: 3, 1 of them hit; its health is 200 of 200 now"
                         " and was 200 at the first shot. Arrows left: 40.")
    raw["entities"][1]["id"] = "other"
    assert "already shot" not in rq.idle_options(raw)["ranged"]


def test_shot_record_survives_a_death_and_reaches_the_frame():
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace()))
    loop._shot_record["d1"] = {"target_id": "d1", "target_type": "ender_dragon", "shots": 5, "hits": 0}
    enriched = loop._frame({"facts": {"health": 20, "health_before": 20}})
    assert enriched["facts"]["shot_record"][0]["shots"] == 5


def test_reaction_timing_starts_with_a_far_visible_hostile_and_keeps_its_first_observation():
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace()))
    raw = {"health": 20, "health_before": 20, "sampled_at_ms": 100, "entities": _mobs("z1", dist=40)}
    loop._observe_reaction_frame(raw)
    first = dict(loop._reaction_observations["z1"])
    assert first["event_distance"] == 40
    assert first["event_sampled_at_ms"] == 100
    chain = loop._reaction_question(raw, time.monotonic())
    assert chain["target"] == "z1" and chain["event_distance"] == 40
    loop._observe_reaction_frame({**raw, "sampled_at_ms": 200, "entities": _mobs("z1", dist=3)})
    assert loop._reaction_observations["z1"] == first
    loop._observe_reaction_frame({**raw, "entities": []})
    assert loop._reaction_observations == {}


def test_threat_call_uses_one_normal_question_and_fighting_again_goes_on():
    raw = facts()
    record = {"tool": "engage_entity", "params": {"target": "near"},
              "supervision_context": {"is_reflex_action": True, "reflex_action_choice": "engage"}}
    tool = SimpleNamespace(name="engage_entity")
    options = CallSupervisor._options_for(tool, record, execution={"state": "running", "facts": raw})
    assert "next_step" not in options and "keep_going" not in options and "engage" in options
    assert set(rq.frame_questions(options, protecting=True)) == {"q"}
    assert "move_away" in options and "call_agent" in options
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace(), trace=SimpleNamespace(emit=lambda *a, **k: None)))
    # Engage again against the same mob carries on; against another mob it is a
    # switch: run-16 04:37:49 "fight the Enderman" went on with a zombie 24 blocks
    # off while the enderman killed the Body (§13). keep_fighting carries on.
    assert loop._continues_protection(record, "engage", {"facts": raw})
    raw["entities"][1]["id"] = "another"
    assert not loop._continues_protection(record, "engage", {"facts": raw})
    assert loop._continues_protection(record, "keep_fighting", {"facts": raw})
    assert not loop._continues_protection(record, "move_away", {"facts": raw})


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


def test_move_away_always_asks_for_more_distance_than_now():
    # Live step5-threat-claude: a fixed 8 blocks returned "already_safe" with
    # a skeleton 8 blocks away; the Body stayed in its range and was shot dead.
    from minebot.app.direct_call_supervision import CallSupervisor
    facts = {"entities": [{"hostile": True, "name": "Skeleton", "dist": 8.2, "pos": [8.2, 64, 0]}]}
    tool, args = CallSupervisor._default_action("move_away", {"facts": facts})
    assert tool == "move_away" and args["danger_pos"] == [8.2, 64.0, 0.0]
    assert args["min_distance"] > 8.2 + 7


def test_running_protection_is_asked_again_only_after_new_damage():
    # User 2026-10-15: asked every second, Decision fled whenever the zombie
    # was not adjacent and never landed a hit (step6-spare-claude2).
    record = {"supervision_context": {"is_reflex_action": True, "reflex_action_choice": "engage"}}
    due = BodyLoop._protection_frame_due
    assert not due(record, {"facts": {"health": 20}})        # just chosen
    assert not due(record, {"facts": {"health": 20}})        # nothing new
    assert not due(record, {"facts": {"health": 20.2}})      # regeneration
    assert due(record, {"facts": {"health": 17.5}})          # hit: choose again
    assert not due(record, {"facts": {"health": 17.5}})
    assert due(record, {"facts": {"health": 15}})
    assert not due(record, {"facts": {}})                    # no health fact: carry on
    work = {"supervision_context": {"intent": "x"}}
    assert due(work, {"facts": {"health": 20}}) and due(work, {"facts": {}})


def _mobs(*ids, dist=6.0):
    return [{"id": i, "name": "Zombie", "hostile": True, "dist": dist} for i in ids]


def test_running_protection_is_asked_again_when_the_visible_mobs_change():
    # E1 (user 2026-10-19): a move_away ran on 47 s after its husk was gone.
    record = {"supervision_context": {"is_reflex_action": True, "reflex_action_choice": "move_away"}}
    due = BodyLoop._protection_frame_due
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z1")}})
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z1", dist=3.0)}})  # same mob, closer
    assert not due(record, {"facts": {"health": 20}})                                          # no entity read
    assert due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z1", "s2")}})           # a second one
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z1", "s2")}})
    assert due(record, {"facts": {"health": 20, "nearby_entities": []}})                       # all gone
    # Far off, coming and going is no new danger (End endermen, body-preemption-20261106.md §8.3).
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", dist=40.0)}})  # visible far away
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", dist=12.0)}})  # same mob, closer
    assert due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", dist=8.0)}})  # walks into reach: once
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", dist=4.0)}})  # closer still
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", dist=40.0)}}) # same mob, farther
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", "z10", dist=40.0)}})
    assert not due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", dist=40.0)}})  # far mob gone
    assert due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", "z11", dist=5.0)}})  # one comes close
    assert due(record, {"facts": {"health": 20, "nearby_entities": _mobs("z9", dist=5.0)}})      # a close one gone
    target = {**record, "params": {"target": "z9"}, "protection_hostiles": {"z9": 30.0}}
    assert due(target, {"facts": {"health": 20, "nearby_entities": []}})                       # its own target gone


def test_running_life_actions_are_still_asked_every_frame():
    record = {"supervision_context": {"is_reflex_action": True, "is_life_action": True,
                                      "reflex_action_choice": "look_player"}}
    assert BodyLoop._protection_frame_due(record, {"facts": {"health": 20}})
    assert BodyLoop._protection_frame_due(record, {"facts": {"health": 20}})


def test_a_protective_action_chosen_again_is_bound_to_the_current_facts(monkeypatch):
    """Re-running the ended call kept the old danger position: move_away
    answered already_safe 40 times while the zombie followed (step5-threat-claude7)."""
    from minebot.app.decision_client import DecisionAnswer

    async def run():
        writes, requests = [], []
        replies = iter(("move_away", "resume"))

        class Decision:
            async def ask(self, request, *, priority=None):
                requests.append(request)
                return DecisionAnswer("unknown", {"answers": {"q": {"choice": next(replies)}}}, .001, len(requests))

        async def run_tool(*args, **kwargs):
            writes.append(args[3])
            return {"success": True, "reason": "moved_away"}

        runtime = SimpleNamespace(
            trace=SimpleNamespace(emit=lambda *a, **k: None), decision_client=Decision(),
            last_known_body_state=facts(3), execution_snapshots=SimpleNamespace(current=lambda: None),
            registry=SimpleNamespace(get=lambda name: SimpleNamespace(name=name)), run_tool=run_tool)
        loop = BodyLoop(runtime)
        record = {"call_id": "work", "tool": "move_to", "params": {}, "questions": 0, "unknown_answers": 0,
                  "supervision_context": {"intent": "reach the requested position"}}
        monkeypatch.setattr("minebot.app.body_loop.FACT_FRAME_INTERVAL_S", .001)
        ended = ("move_away", {"danger_pos": [20.0, 64.0, 0.0], "min_distance": 9.0})
        choice = await loop._after_default_action(record, ended, {"success": True, "reason": "already_safe"},
                                                  "away-call", protective_choice="move_away")
        assert choice == "resume"
        # Bound to the threat where it is now, and one whole run from every
        # hostile seen (body-tool-boundaries-20261110 §7), not the old one-step dodge.
        assert len(writes) == 1
        assert writes[0]["danger_pos"] == [3.0, 64.0, 0.0] and writes[0]["from_hostiles"] is True
        assert "threat_kind" not in writes[0]

    asyncio.run(run())


def test_leaving_lava_heads_for_the_named_exit_and_fire_is_put_out_where_the_body_stands():
    from minebot.app.direct_call_supervision import CallSupervisor
    raw = {"in_lava": True, "lava_exit": [-15, -55, 25], "position": [-16.6, -55.0, 25.4]}
    assert CallSupervisor._default_action("leave_lava", {"facts": raw}) == (
        "__reflex_leave_lava", {"pos": [-15, -55, 25], "timeout_s": 5.0})
    assert CallSupervisor._default_action("leave_lava", {"facts": {**raw, "lava_exit": None}}) is None
    burning = {"on_fire": True, "standing_on": [-16, -56, 24], "position": [-16.9, -55.0, 24.5]}
    assert CallSupervisor._default_action("put_out_fire", {"facts": burning}) == (
        "use_on_block", {"item": "water_bucket", "pos": [-16, -55, 24]})


def test_a_step_walks_to_the_cell_the_body_named_for_that_direction():
    from minebot.app.direct_call_supervision import CallSupervisor
    steps = [{"direction": "north", "to": [4, 64, -6], "cells": 4}]
    assert CallSupervisor._default_action("step_north", {"facts": {"steps": steps}}) == (
        "__reflex_step", {"pos": [4, 64, -6], "timeout_s": 3.0})
    assert CallSupervisor._default_action("step_east", {"facts": {"steps": steps}}) is None


def test_cushion_fall_runs_the_bodys_timed_pour():
    raw = {"dimension": "minecraft:the_end", "inventory": {"water_bucket": 1},
           "fall": {"fallen": 6.0, "ground_below": 22.5, "lands_on": "end_stone"}}
    assert CallSupervisor._default_action("cushion_fall", {"facts": raw}) == (
        "__reflex_cushion_fall", {"item": "water_bucket", "timeout_s": 10.0})
    assert CallSupervisor._default_action("cushion_fall", {"facts": {**raw, "fall": {"void_below": True}}}) is None


def test_scoop_water_uses_the_empty_bucket_looking_down():
    raw = {"inventory": {"bucket": 1}, "water_source_in_reach": [4, 64, -2]}
    assert CallSupervisor._default_action("scoop_water", {"facts": raw}) == (
        "__reflex_use_item", {"item": "bucket", "look_at": [4.5, 64.4, -1.5]})
    assert CallSupervisor._default_action("scoop_water", {"facts": {**raw, "water_source_in_reach": None}}) is None


def test_each_arrow_is_logged_with_who_loosed_it():
    # dragon-unit-tests-20261030 §6.y.41: the Agent never learned Decision spent the arrows.
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace()))
    now = time.monotonic()
    loop._reflex_spans.append({"by": "decision", "start": now - 3, "end": None})
    shot = {"target_id": "d1", "target_type": "ender_dragon", "age_s": 1.0, "outcome": "missed",
            "health_before": 200.0}
    loop._frame({"facts": {"health": 20, "health_before": 20, "last_shot": shot}})
    loop._frame({"facts": {"health": 20, "health_before": 20, "last_shot": {**shot, "age_s": 1.1}}})
    loop._reflex_spans[-1]["end"] = now - 2
    later = {"target_id": "c1", "target_type": "end_crystal", "age_s": 0.0, "outcome": "hit"}
    loop._frame({"facts": {"health": 20, "health_before": 20, "last_shot": later}})
    log = loop.shot_log()
    assert [(s["target_type"], s["by"], s["hit"]) for s in log] == [
        ("ender_dragon", "decision", False), ("end_crystal", "you", True)]


def test_the_dragon_shot_says_the_crystals_still_standing_heal_it():
    # dragon-unit-tests-20261030 §6.y.43: a fact the Body sees, not a block on the shot.
    raw = facts(inventory={"minecraft:bow": 1, "minecraft:arrow": 40})
    raw["entities"][1].update(id="d1", name="Ender Dragon", type="minecraft:ender_dragon", boss=True,
                              health=200.0, max_health=200.0)
    assert "end crystal" not in rq.idle_options(raw)["ranged"]
    raw["entities"].append({"id": "c1", "name": "End Crystal", "type": "end_crystal", "pos": [40, 90, 0]})
    assert rq.idle_options(raw)["ranged"].endswith(
        "Arrows left: 40. 1 end crystal still stands within 64 blocks; while it stands, it heals the dragon back to full.")
    raw["entities"] += [{"id": "c2", "type": "minecraft:end_crystal"}, {"id": "c2", "type": "end_crystal"}]
    assert rq.idle_options(raw)["ranged"].endswith(
        "2 end crystals still stand within 64 blocks; while any stands, they heal the dragon back to full.")


def test_a_seen_mob_walking_in_is_asked_about_once():
    """run-37 33:03: a creeper seen 25 blocks off came to the Body while `surface` ran; 27 s, no frame."""
    record = {"supervision_context": {"is_reflex_action": True, "reflex_action_choice": "surface"}}
    due = BodyLoop._protection_frame_due

    def creeper(dist, *others):
        return [{"id": "c1", "type": "creeper", "name": "Creeper", "hostile": True, "dist": dist}, *others]

    assert not due(record, {"facts": {"health": 20, "nearby_entities": creeper(25.0)}})
    assert not due(record, {"facts": {"health": 20, "nearby_entities": creeper(13.3)}})
    assert due(record, {"facts": {"health": 20, "nearby_entities": creeper(11.1)}})   # within a blast
    assert not due(record, {"facts": {"health": 20, "nearby_entities": creeper(7.4)}})
    assert not due(record, {"facts": {"health": 20, "nearby_entities": creeper(9.0)}})  # backs off and on: still once
    spider = {"id": "s1", "type": "spider", "name": "Spider", "hostile": True, "dist": 12.2}
    assert not due(record, {"facts": {"health": 20, "nearby_entities": creeper(9.0, spider)}})
    assert due(record, {"facts": {"health": 20, "nearby_entities": creeper(9.0, {**spider, "dist": 4.6})}})
    # Those near when the action was chosen are not asked about again.
    fight = {"supervision_context": {"is_reflex_action": True, "reflex_action_choice": "engage"}}
    assert not due(fight, {"facts": {"health": 20, "nearby_entities": [{**spider, "dist": 3.0}]}})
    assert not due(fight, {"facts": {"health": 20, "nearby_entities": [{**spider, "dist": 1.5}]}})
