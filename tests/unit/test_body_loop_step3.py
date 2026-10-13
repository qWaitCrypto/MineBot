"""Deterministic history/safety plumbing, not model acceptance evidence."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from minebot.app import reflex_question as rq
from minebot.app.body_loop import BodyLoop
from minebot.app.execution_snapshot import ExecutionSnapshots
from minebot.app.work_runtime import WorkPlanExecutor
from minebot.game.real_client_body import RealClientBody


def runtime():
    events = []
    return SimpleNamespace(
        trace=SimpleNamespace(emit=lambda event, **payload: events.append({"event": event, **payload})),
        agent_context=SimpleNamespace(goal_text="Have four logs"),
        last_known_body_state={}, direct_call_handoffs={},
        decision_client=SimpleNamespace(),
    ), events


def frame(loop, clock, **facts):
    with patch("time.monotonic", return_value=clock):
        return loop._frame({"facts": {"health": 20, "oxygen": 300, "food": 20,
                                      "position": [1, 64, 2], **facts}})


def test_safe_standing_position_tracks_only_observed_dry_uninjured_ground():
    owner, _ = runtime()
    loop = BodyLoop(owner)
    frame(loop, 100, on_ground=True, head_in_liquid=False)
    assert loop.safe_standing_position is None  # no health baseline yet
    safe = frame(loop, 105, on_ground=True, head_in_liquid=False)
    assert safe["facts"]["safe_standing_position"] == [1, 64, 2]
    wet = frame(loop, 106, on_ground=True, head_in_liquid=True, oxygen=200, position=[4, 60, 4])
    assert wet["facts"]["safe_standing_position"] == [1, 64, 2]
    hurt = frame(loop, 107, on_ground=True, head_in_liquid=False, health=18, position=[4, 65, 4])
    assert hurt["facts"]["safe_standing_position"] == [1, 64, 2]
    frame(loop, 108, on_ground=False, head_in_liquid=False, position=[9, 64, 9])
    frame(loop, 109, on_ground=True, position=[9, 64, 9])
    assert loop.safe_standing_position == [1, 64, 2]
    restored = frame(loop, 110, on_ground=True, head_in_liquid=False, position=[9, 64, 9])
    assert restored["facts"]["safe_standing_position"] == [9, 64, 9]
    assert restored["_frame_input"]["safe_standing_position"] == [1, 64, 2]
    # Coordinates, vitals and eligibility survive a call boundary unchanged.
    loop.current = None
    assert frame(loop, 111, head_in_liquid=True)["facts"]["safe_standing_position"] == [9, 64, 9]


def test_new_raw_frame_replay_restores_the_previous_safe_position():
    owner, _ = runtime()
    loop = BodyLoop(owner)
    frame(loop, 100, on_ground=True, head_in_liquid=False)
    frame(loop, 105, on_ground=True, head_in_liquid=False)
    original = frame(loop, 106, head_in_liquid=True, oxygen=200)
    saved = original["_frame_input"]
    replica = BodyLoop(owner)
    replica._vitals._samples = copy.deepcopy(saved["vital_samples"])
    replica.safe_standing_position = copy.deepcopy(saved["safe_standing_position"])
    with patch("time.monotonic", return_value=saved["monotonic_s"]):
        assert replica._frame(saved["execution"]) == original


@pytest.mark.parametrize("oxygen", [-10, 0, 139, 299])
def test_head_out_observation_is_not_overridden_by_a_falling_air_baseline(oxygen):
    body = rq._body_sentence({"health": 20, "food": 20, "head_in_liquid": False,
                              "oxygen": oxygen, "oxygen_before": 300, "oxygen_age_s": 5})
    assert "head is out of liquid" in body
    assert "under water" not in body and "drowning" not in body


def test_snapshot_preserves_native_ground_and_eye_contact_facts():
    snapshots = ExecutionSnapshots()
    snapshots.begin("call", "move_to")
    snapshots.observe("call", {"position": [1, 64, 2], "on_ground": True, "head_in_liquid": False})
    assert snapshots.current()["facts"]["on_ground"] is True
    assert snapshots.current()["facts"]["head_in_liquid"] is False


def test_idle_uses_unprojected_native_facts_through_the_existing_read_lane():
    async def run():
        owner, _ = runtime()
        raw = {"on_ground": True, "head_in_liquid": False, "health": 20, "food": 20,
               "oxygen": 300, "position": [1, 64, 2], "inventory": {}}
        native = RealClientBody(bot_name="Bot", ipc=SimpleNamespace(
            connect=lambda: None, state=lambda: copy.deepcopy(raw)))
        owner.body = SimpleNamespace(get_reflex_facts=native.get_reflex_facts,
                                    get_state=lambda: pytest.fail("projected reader used"))
        async def read(callback, *args):
            return callback(*args)
        owner._read_lane = SimpleNamespace(run=read)
        loop = BodyLoop(owner)
        with patch("time.monotonic", return_value=100):
            first = await loop._read_idle_facts()
        with patch("time.monotonic", return_value=105):
            second = await loop._read_idle_facts()
        assert first["on_ground"] and first["head_in_liquid"] is False
        assert second["safe_standing_position"] == [1, 64, 2]
    asyncio.run(run())


def test_direct_interruptions_reach_the_actual_result_question_and_agent_handoff():
    async def run():
        owner, events = runtime()
        owner.registry = SimpleNamespace(get=lambda name: SimpleNamespace(name=name))
        stops, notifications, requests = [], [], []
        async def stop(*args, **kwargs):
            stops.append(kwargs)
            return {"settled": True}
        owner.cancel_active_execution_with_facts = stop
        owner.notify_reflex_fact = notifications.append
        loop = BodyLoop(owner)
        record = {"call_id": "mine", "tool": "collect_resource", "params": {"item": "logs", "count": 4},
                  "supervision_context": {"intent": "Have four logs"}, "questions": 0,
                  "history": [], "history_total": 0, "started_at": 0, "timeout_s": None,
                  "result": {"success": False, "reason": "preempted", "canRetry": True}}
        execution = {"tool": "collect_resource", "facts": {"position": [1, 60, 2], "inventory": {}}}
        action = ("go_to_surface", {})
        async def protection(choice, action, *args, **kwargs):
            loop._set_state("survival", "decision")
            return choice, action, {"success": True, "reason": "surface_reached"}, "surface"
        loop._execute_protection = protection
        async def facts():
            return {"health": 20, "food": 20, "oxygen": 270, "head_in_liquid": False,
                    "inventory": {}, "position": [1, 65, 2]}
        loop._read_idle_facts = facts
        async def judge(call_id, tool, params, execution, idle_s, **kwargs):
            request = loop._question(call_id, tool, params, execution, idle_s=idle_s,
                                    purpose=kwargs["purpose"], options=kwargs["options"],
                                    supervision_context=kwargs["supervision_context"],
                                    decision_context=kwargs["decision_context"])
            requests.append(request)
            assert loop.loop_state == "survival"
            return "resume" if len(requests) < 7 else "call_agent"
        loop.judge = judge
        for _ in range(7):
            loop.current = record
            record["cancel_requested"] = record["settling"] = False
            await loop._run_default_action(record, execution, "surface", action)
        assert len(stops) == 7 and all(s["invalidate_generation"] is False for s in stops)
        assert len(notifications) == 1
        assert record["history_total"] == 7 and len(record["history"]) == 5
        for index, request in enumerate(requests, 1):
            lines = request["state"]["interruptions_of_the_original_work"]
            if index > 5:
                assert lines.pop(0) == f"There were {index - 5} earlier attempts before these."
            assert len(lines) == min(index, 5)
            assert all("preempted" in line and "go_to_surface()" in line for line in lines)
            assert "under water" not in request["state"]["body"]
            assert set(request["questions"]) == {"q"}
        assert len(notifications[0]["history"]) == 5
        assert notifications[0]["history_earlier"] == 2
        assert len([e for e in events if e["event"] == "call_supervision_default_finished"]) == 7
    asyncio.run(run())


def test_protective_timeout_still_records_the_already_settled_original_attempt():
    async def run():
        owner, _ = runtime()
        async def stop(*args, **kwargs):
            return {"settled": True}
        owner.cancel_active_execution_with_facts = stop
        loop = BodyLoop(owner)
        record = {"call_id": "mine", "tool": "collect_resource", "params": {},
                  "supervision_context": {"intent": "Have four logs"},
                  "started_at": asyncio.get_running_loop().time(), "timeout_s": .02,
                  "cancel_requested": False, "result": {"success": False, "reason": "preempted"}}
        loop.current = record
        async def protection(*args, **kwargs):
            await asyncio.sleep(10)
        loop._execute_protection = protection
        await loop._protect_work(record, {"tool": "collect_resource", "facts": {
            "position": [1, 60, 2], "inventory": {}}}, "surface", ("go_to_surface", {}))
        handoff = owner.direct_call_handoffs["mine"]["facts"]
        assert len(handoff["history"]) == record["history_total"] == 1
        assert handoff["history"][0]["result"]["reason"] == "preempted"
        assert handoff["default_action"]["then"] == "unknown"
        assert loop._suspended is None
    asyncio.run(run())


def test_interruption_preserves_late_original_terminal_and_redirected_protective_retry():
    async def run():
        owner, _ = runtime()
        owner.registry = SimpleNamespace(get=lambda name: SimpleNamespace(name=name))
        notifications, requests, calls = [], [], []
        owner.notify_reflex_fact = notifications.append
        async def stop(*args, **kwargs):
            return {"settled": True}
        owner.cancel_active_execution_with_facts = stop
        loop = BodyLoop(owner)
        record = {"call_id": "mine", "tool": "collect_resource", "params": {},
                  "supervision_context": {"intent": "Have four logs"}, "questions": 0,
                  "cancel_requested": False, "timeout_s": None}
        loop.current = record
        async def run_tool(tool, *args, **kwargs):
            calls.append(tool.name)
            record["result"] = {"success": False, "reason": "preempted", "canRetry": True}
            if len(calls) == 2:
                loop._protective_redirect = ("retreat_safe", ("move_to", {"pos": [1, 65, 2]}))
            return {"success": len(calls) == 3,
                    "reason": "arrived" if len(calls) == 3 else "swim_up_blocked"}
        owner.run_tool = run_tool
        async def facts():
            return {"health": 20, "food": 20, "oxygen": 300, "position": [1, 65, 2]}
        loop._read_idle_facts = facts
        async def judge(*args, **kwargs):
            history = kwargs["decision_context"]["interrupted_history"]
            requests.append(copy.deepcopy(history))
            return "repeat" if len(requests) == 1 else "call_agent"
        loop.judge = judge
        await loop._run_default_action(record, {"tool": "collect_resource", "facts": {
            "position": [1, 60, 2], "inventory": {}}}, "surface", ("go_to_surface", {}))
        assert calls == ["go_to_surface", "go_to_surface", "move_to"]
        assert all(h[0]["result"]["reason"] == "preempted" for h in requests)
        default = notifications[0]["default_action"]
        assert default["tool"] == "move_to" and default["result"]["reason"] == "arrived"
        stored = notifications[0]["history"][0]["result"]["metrics"]["direct_call_handoff"]
        assert stored["facts"]["default_action"]["tool"] == "move_to"
        assert stored["facts"]["default_action"]["result"]["reason"] == "arrived"
    asyncio.run(run())


def test_plan_reentry_inherits_history_even_when_projection_removed_metrics():
    async def run():
        plan = {"language": "minebot-work/0.2", "goal": "Have four logs", "intent": "Have four logs",
                "timeout_s": 60, "work": {"kind": "call", "id": "mine", "tool": "collect_resource",
                "intent": "Have four logs", "arguments": {"item": "logs", "count": 4}}}
        contexts = []
        async def call(tool, args, *, call_id, timeout_s):
            contexts.append(executor.call_context(call_id))
            choice = "repeat" if len(contexts) < 3 else "call_agent"
            return {"success": False, "reason": "preempted", "canRetry": True,
                    "_supervision_handoff": {"facts": {"decision": {"choice": choice},
                    "reason": "decision_default_tool", "default_action": {"tool": "go_to_surface",
                    "result": {"success": True, "reason": "surface_reached"}},
                    "history": [{"body": {"position": [1, 60, 2], "inventory": {}},
                                 "previous": "must not be recursively copied"}]}}}
        async def state():
            return {"inventory_counts": {}, "pos": [1, 60, 2]}
        executor = WorkPlanExecutor(plan, call=call, state=state)
        outcome = await executor.run()
        assert outcome.status == "blocked"
        assert "history" not in contexts[0]
        assert len(contexts[1]["history"]) == 1 and len(contexts[2]["history"]) == 2
        for entry in contexts[2]["history"]:
            assert entry["body"]["position"] == [1, 60, 2]
            facts = entry["result"]["metrics"]["direct_call_handoff"]["facts"]
            assert "history" not in facts
            assert facts["default_action"]["tool"] == "go_to_surface"
        assert "go_to_surface()" in rq.attempt_lines(executor._call_histories["mine"])[0]
    asyncio.run(run())


def test_interruption_history_keeps_five_recent_entries_and_reports_earlier_count():
    entry = {"result": {"success": False, "reason": "preempted", "metrics": {"direct_call_handoff": {
        "facts": {"default_action": {"tool": "go_to_surface", "result": {"success": True}}}}}},
        "body": {"position": [1, 60, 2], "inventory": {}}}
    lines = rq.interruption_lines([entry] * 5, earlier=7)
    assert lines[0] == "There were 7 earlier attempts before these."
    assert lines[1].startswith("Attempt 8:") and lines[-1].startswith("Attempt 12:")
    assert "call Agent" not in " ".join(lines)
