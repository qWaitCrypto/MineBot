"""Terminal arbitration/cancellation through the Body's lifecycle, not model acceptance."""
import asyncio
import time
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop
from minebot.contract import Event


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
