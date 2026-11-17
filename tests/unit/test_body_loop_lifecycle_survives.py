"""A call that ends while Decision is still thinking must not end the lifecycle loop.

Villa run 2026-10-16 (DeepSeek resume): at 12:41:25 Decision request 975 was in
flight for a running build. The Agent cancelled the reflex activity at 12:41:26.558
and the supervisor, finishing a millisecond later, cancelled that request. The
monitor read the cancelled task with ``.result()``, which raises CancelledError, a
BaseException that ``except Exception`` lets through. The one lifecycle task ended,
silently and still "started", so no frame was ever asked again and every later
call that needed a monitor failed with "BodyLoop already has an active monitor"
(69 failures in two hours; 0 of 57 place_block calls reported success).
"""
import asyncio
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop


def _loop():
    events = []
    native = {'operation_id': 'job', 'state': 'running', 'phase': 'working',
              'fact_revision': 1, 'facts': {'position': [10, 64, 0], 'health': 20, 'food': 16}}
    holder = []

    def current():
        # The running snapshot belongs to whichever call the loop is on.
        call = (holder[0].current or {}).get('call_id', 'job') if holder else 'job'
        return {**native, 'operation_id': call}

    runtime = SimpleNamespace(
        trace=SimpleNamespace(emit=lambda event, **kw: events.append((event, kw))),
        decision_client=SimpleNamespace(), direct_call_handoffs={},
        execution_snapshots=SimpleNamespace(current=current, last=lambda: None,
                                            tick=lambda call_id: None))
    loop = BodyLoop(runtime)
    holder.append(loop)
    return loop, events


def _record(call_id='job'):
    return {'call_id': call_id, 'tool': 'build_structure', 'params': {},
            'cancel_requested': False, 'questions': 0, 'reads': 0, 'unknown_answers': 0,
            'initial_reader_done': True, 'supervision_context': {}}


def test_monitor_returns_when_its_decision_is_withdrawn_by_the_call_ending():
    async def run():
        loop, _ = _loop()
        loop.current = record = _record()
        finish = asyncio.Event()
        worker = asyncio.create_task(finish.wait())
        wake = asyncio.Event()
        wake.set()
        entered = asyncio.Event()

        async def judge(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        loop.judge = judge
        monitor = asyncio.create_task(loop._monitor(
            record, SimpleNamespace(name='build_structure'), record['params'], wake, worker))
        try:
            await asyncio.wait_for(entered.wait(), .5)
            # What CallSupervisor.run's finally does when the call is cancelled.
            record['decision_task'].cancel()
            await asyncio.gather(record['decision_task'], return_exceptions=True)
            record['cancel_requested'] = True
            await asyncio.wait({monitor}, timeout=.5)
            assert monitor.done()
            assert not monitor.cancelled(), 'the monitor died with CancelledError'
            assert monitor.exception() is None
        finally:
            monitor.cancel()
            worker.cancel()
            await asyncio.gather(monitor, worker, return_exceptions=True)
    asyncio.run(run())


def test_the_next_call_is_supervised_after_the_first_ends_with_a_withdrawn_decision():
    async def run():
        loop, events = _loop()
        asked = []

        async def judge(call_id, *args, **kwargs):
            asked.append(call_id)
            await asyncio.Event().wait()

        loop.judge = judge
        loop._result_pending['parked'] = {}  # keep the idle branch from reading the Body
        await loop.start()
        tool = SimpleNamespace(name='build_structure')
        try:
            first = _record('first')
            loop.current = first
            first_worker = asyncio.create_task(asyncio.Event().wait())
            wake = asyncio.Event()
            wake.set()
            loop._start_monitor(first, tool, first['params'], wake, first_worker)
            for _ in range(100):
                if first.get('decision_task') is not None:
                    break
                await asyncio.sleep(.01)
            assert first.get('decision_task') is not None, [e for e, _ in events]
            # The Agent cancels the activity: the supervisor ends the call and
            # withdraws the question that was in flight.
            first['cancel_requested'] = True
            first['decision_task'].cancel()
            first_worker.cancel()
            await asyncio.wait_for(first['monitor_done'].wait(), 1)
            assert loop._active_monitor is None
            assert not loop._lifecycle_task.done(), 'the lifecycle task ended'

            second = _record('second')
            loop.current = second
            second_worker = asyncio.create_task(asyncio.sleep(.2))
            wake2 = asyncio.Event()
            wake2.set()
            loop._start_monitor(second, tool, second['params'], wake2, second_worker)
            await asyncio.wait_for(second['monitor_done'].wait(), 2)
            assert 'second' in asked
            assert loop._active_monitor is None
        finally:
            await loop.stop()
    asyncio.run(run())


def test_an_error_that_leaves_the_lifecycle_loop_is_reported_and_the_loop_restarts(monkeypatch):
    import minebot.app.body_loop as module
    monkeypatch.setattr(module, 'LIFECYCLE_RESTART_DELAY_S', .01)
    monkeypatch.setattr(module, 'FACT_FRAME_INTERVAL_S', .02)

    async def run(raised):
        loop, events = _loop()
        loop.runtime.execution_snapshots.current = lambda: None
        calls = []
        reached_third = asyncio.Event()

        async def read_idle_facts():
            calls.append(1)
            if len(calls) == 1:
                raise raised
            reached_third.set()
            await asyncio.Event().wait()

        loop._read_idle_facts = read_idle_facts
        await loop.start()
        try:
            await asyncio.wait_for(reached_third.wait(), 2)
            failed = [kw for event, kw in events if event == 'reflex_loop_failed']
            assert len(failed) == 1 and failed[0]['error_type'] == type(raised).__name__
            assert loop._lifecycle_started and not loop._lifecycle_task.done()
        finally:
            await loop.stop()
    asyncio.run(run(RuntimeError('boom')))
    # A CancelledError that no stop request caused (a child task's) is an error too.
    asyncio.run(run(asyncio.CancelledError()))


def test_stop_still_ends_the_lifecycle_task():
    async def run():
        loop, events = _loop()
        loop._result_pending['parked'] = {}
        await loop.start()
        task = loop._lifecycle_task
        await loop.stop()
        assert task.done()
        assert not any(event == 'reflex_loop_failed' for event, _ in events)
        assert not loop._lifecycle_started
    asyncio.run(run())


def test_a_call_that_cannot_be_monitored_is_stopped_not_left_running():
    async def run():
        loop, _ = _loop()
        loop._active_monitor = (_record('other'), None, {}, asyncio.Event(), None)  # a monitor is busy
        record = _record('late')
        worker = asyncio.create_task(asyncio.Event().wait())
        try:
            try:
                await loop._register_monitor(record, SimpleNamespace(name='place_block'), {}, asyncio.Event(), worker)
            except RuntimeError as exc:
                assert 'active monitor' in str(exc)
            else:
                raise AssertionError('the second monitor was accepted')
            assert worker.cancelled(), 'the call kept running without a monitor'
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())
