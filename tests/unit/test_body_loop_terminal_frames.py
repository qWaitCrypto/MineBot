"""Terminal arbitration/cancellation through the Body's lifecycle, not model acceptance."""
import asyncio
import copy
import time
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop
from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionAnswer
from minebot.contract import Event
from minebot.game.governance import GovernancePolicy


def owner():
    events = []
    runtime = SimpleNamespace(
        trace=SimpleNamespace(emit=lambda event, **kw: events.append((event, kw))),
        decision_client=SimpleNamespace(),
        last_known_body_state={"health": 20, "food": 20, "oxygen": 300},
        execution_snapshots=SimpleNamespace(current=lambda: None, last=lambda: None),
        registry=SimpleNamespace(get=lambda name: SimpleNamespace(name=name)),
        direct_call_handoffs={},
    )
    return BodyLoop(runtime), runtime, events


def test_direct_plan_and_protective_terminals_use_one_consumer():
    async def run():
        loop, _, _ = owner()
        active = peak = 0
        purposes, consumers = [], []
        entered, release = asyncio.Event(), asyncio.Event()
        original = loop._serve_terminal_question

        async def serve(*args):
            consumers.append(asyncio.current_task())
            await original(*args)

        async def judge(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            purposes.append(kwargs['purpose'])
            entered.set()
            await release.wait()
            active -= 1
            return 'next_step' if kwargs['purpose'] != 'default_result_frame' else 'resume'

        loop._serve_terminal_question = serve
        loop.judge = judge
        # These are result facts, so the existing result slot prevents idle
        # work throughout dispatch. No second Body claim or state lock exists.
        loop._result_pending['direct'] = {}
        record = {'call_id': 'direct', 'tool': 'move_to', 'params': {}, 'questions': 0,
                  'supervision_context': {'intent': 'Reach the point'}}
        protective = {**record, 'call_id': 'suspended'}
        tool = SimpleNamespace(name='move_to')
        jobs = [
            asyncio.create_task(loop._decide_result(record, tool, {}, {'success': True})),
            asyncio.create_task(loop.decide_plan_result(
                call_id='plan', tool=tool, params={}, plan_context={'intent': 'Reach the point'},
                decision_context={'tool_result': {'success': True}, 'body_state': {}},
            )),
            asyncio.create_task(loop._after_default_action(
                protective, ('go_to_surface', {}), {'success': True}, 'surface')),
        ]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(.01)
            assert len(purposes) == 1  # queued requests cannot run in parallel
            release.set()
            assert await asyncio.wait_for(asyncio.gather(*jobs), 1) == ['step_done', 'next_step', 'resume']
            assert peak == 1
            assert set(purposes) == {'result_frame', 'plan_result_frame', 'default_result_frame'}
            assert len(consumers) == 3
            assert all(task is loop._lifecycle_task for task in consumers)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await loop.stop()
    asyncio.run(run())


def test_original_deadline_cancels_inflight_terminal_and_releases_result_slot():
    async def run():
        loop, _, _ = owner()
        cancelled = asyncio.Event()

        async def judge(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        loop.judge = judge
        started = time.monotonic()
        record = {'call_id': 'original', 'tool': 'move_to', 'params': {},
                  'started_at': started, 'timeout_s': .05, 'questions': 0,
                  'supervision_context': {'intent': 'Reach the point'}}
        try:
            result = await asyncio.wait_for(loop._after_default_action(
                record, ('go_to_surface', {}), {'success': True}, 'surface'), .5)
            assert result == 'unknown'
            await asyncio.wait_for(cancelled.wait(), .2)
            assert time.monotonic() - started < .4
            assert not loop._result_pending
        finally:
            await loop.stop()
    asyncio.run(run())


def test_death_during_terminal_request_is_consumed_without_waiting_for_decision():
    async def run():
        loop, runtime, events = owner()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        notifications = []

        async def stop_body(*args, **kwargs):
            return {'settled': True}

        async def judge(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runtime.cancel_active_execution_with_facts = stop_body
        runtime.notify_body_lifecycle_event = notifications.append
        loop.judge = judge
        job = asyncio.create_task(loop.decide_plan_result(
            call_id='original', tool=SimpleNamespace(name='move_to'), params={},
            plan_context={'intent': 'Reach the point'},
            decision_context={'tool_result': {'success': True}, 'body_state': {}},
        ))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            loop._queue_lifecycle_event(Event(seq=1, tick=1, bot='Bot', name='death', data={'health': 0}))
            assert await asyncio.wait_for(job, .2) == 'unknown'
            await asyncio.wait_for(cancelled.wait(), .2)
            assert loop.loop_state == 'dead'
            assert len(notifications) == 1
            assert sum(event == 'reflex_death' for event, _ in events) == 1
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            await loop.stop()
    asyncio.run(run())


def test_plan_frame_after_decision_uses_a_bed_reports_the_use_and_drops_the_exploded_bed():
    async def run():
        loop, runtime, _ = owner()
        dimension = "minecraft:the_end"
        policy = runtime.governance_policy = GovernancePolicy(dimension=lambda: dimension)
        policy.record_bot_placement((0, 68, 0), "white_bed", "place", "Bot")
        facts = {"dimension": dimension, "position": [0.5, 68, 1.5], "health": 20,
                 "entities": [{"type": "ender_dragon", "hostile": True, "dist": 5}]}
        # The historical receipt also populated the life-local fallback.
        loop._frame({"facts": facts, "result": {"metrics": {"world_edit_receipt": {
            "edits": [{"pos": [0, 68, 0], "before": "air", "after": "white_bed"}]}}}})
        placed = {"success": True, "reason": "placed_server_confirmed"}
        used = {"success": True, "reason": "completed", "metrics": {
            "target": [0, 68, 0], "dimension": dimension,
            "target_before": {"type": "white_bed"}, "target_after": {"type": "air"}}}
        params = {"block_type": "white_bed", "pos": [0, 68, 0]}
        loop.observe_call_result("place_block", params, placed)
        requests, compiled = [], []

        async def ask(request, **kwargs):
            requests.append(copy.deepcopy(request))
            choice = "use_bed_0_68_0" if len(requests) == 1 else "next_step"
            return DecisionAnswer(choice, {"answers": {"q": {"choice": choice}}}, .001, len(requests))

        async def lane(callback, *, timeout_s=None):
            return callback()

        async def stop_body(*args, **kwargs):
            return {"settled": True}

        async def run_tool(tool, callback, *args, **kwargs):
            assert tool.name == "use_on_block"
            facts["health"] = 16.6
            # Exercise the actual call completion path, not a manually
            # injected result or a new plan-result question for the use.
            return await loop.run(tool, lambda *args: used, args,
                                  call_id=kwargs["tool_call_id"], timeout_s=1,
                                  supervision_context=kwargs["supervision_context"])

        async def body_now():
            return dict(facts)

        def compile_frame(current):
            options = {**rq.end_bed_options(current), "next_step": "Continue the plan", "call_agent": "Ask the Agent"}
            question = {"state": rq.frame_state(goal="", intent="Place a bed", tool="place_block",
                                                params=params, facts=current, running=False, result=placed),
                        "questions": rq.frame_questions(options)}
            compiled.append(copy.deepcopy(question))
            return question

        loop._client = SimpleNamespace(ask=ask)
        runtime.execution_lane = SimpleNamespace(run=lane)
        runtime.execution_snapshots.running = lambda call_id: None
        runtime.cancel_active_execution_with_facts = stop_body
        runtime.run_tool = run_tool
        loop._body_now = body_now
        try:
            choice = await asyncio.wait_for(loop.decide_plan_result(
                call_id="place", tool=SimpleNamespace(name="place_block"), params=params,
                plan_context={"is_plan": True, "intent": "Place a bed"},
                decision_context={"tool_result": placed, "body_state": facts}, compile=compile_frame), 2)
            assert choice == "next_step"
            assert len(requests) == 2
            first, after = requests
            assert "use_bed_0_68_0" in first["questions"]["q"]["criteria"]
            assert "use_bed_0_68_0" not in after["questions"]["q"]["criteria"]
            assert not policy.bot_placements
            assert "use_on_block(" in after["state"]["finished_call"]
            assert "has ended normally" in after["state"]["finished_call"]
            assert "place_block(" in after["state"]["original_call_result"]
            # Decorating the outgoing frame must not rewrite the plan's saved question.
            assert "place_block(" in compiled[-1]["state"]["finished_call"]
            # Idle frames carry no old result as news.
            assert "finished_call" not in loop._idle_question(facts, {"stay": "Stay"})["state"]
        finally:
            await loop.stop()
    asyncio.run(run())


def test_running_work_reflex_and_life_frames_keep_the_latest_finished_result():
    loop, _, _ = owner()
    loop.observe_call_result("use_on_block", {"pos": [0, 68, 0]},
                             {"success": False, "reason": "targeted_use_unverified"})
    for context in ({"intent": "Travel"}, {"is_reflex_action": True}, {"is_life_action": True}):
        request = loop._question("move", SimpleNamespace(name="move_to"), {}, {"facts": {}},
                                 idle_s=0, purpose="running_frame", supervision_context=context,
                                 options={"keep_going": "Continue", "call_agent": "Ask the Agent"})
        assert "use_on_block(" in request["state"]["finished_call"]
        assert "targeted_use_unverified" in request["state"]["finished_call"]


def test_receiving_an_older_worker_result_does_not_overwrite_a_later_completion():
    async def run():
        loop, runtime, _ = owner()
        runtime.execution_snapshots.running = lambda call_id: None

        async def lane(callback, *, timeout_s=None):
            result = callback()
            # Another call finished after the worker did, before the caller
            # gets its turn to await and judge that older result.
            loop.observe_call_result("use_on_block", {"pos": [0, 68, 0]},
                                     {"success": False, "reason": "targeted_use_unverified"})
            return result

        runtime.execution_lane = SimpleNamespace(run=lane)
        try:
            await loop.run(SimpleNamespace(name="place_block"), lambda *args: {"success": True},
                           (None, {"pos": [0, 68, 0]}), call_id="place", timeout_s=1,
                           supervision_context={"is_plan": True})
            assert "use_on_block(" in loop._latest_finished_call
            assert "targeted_use_unverified" in loop._latest_finished_call
        finally:
            await loop.stop()
    asyncio.run(run())
