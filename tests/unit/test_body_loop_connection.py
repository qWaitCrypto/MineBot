import asyncio
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop
from minebot.contract import Event
from minebot.game.real_client_body import RealClientBody
from test_body_loop import _runtime


def test_native_connection_events_use_existing_lifecycle_listener():
    body = RealClientBody('test', SimpleNamespace(set_event_listener=lambda listener: None))
    observed = []
    body.add_lifecycle_listener(observed.append)
    for seq, name in enumerate(('world_ready', 'world_disconnected', 'world_ready'), 1):
        raw = {'epoch': 'client', 'seq': seq, 'tick': seq, 'name': name, 'data': {'ready': name == 'world_ready'}}
        body._on_lifecycle_event(raw)
        body._on_lifecycle_event(raw)
    assert [event.name for event in observed] == ['world_ready', 'world_disconnected', 'world_ready']


def test_disconnect_finishes_consumer_and_reconnect_starts_one_successor(monkeypatch):
    async def run():
        runtime, trace, decision = _runtime()
        listeners = []
        runtime.body = SimpleNamespace(add_lifecycle_listener=listeners.append,
                                       remove_lifecycle_listener=listeners.remove)
        monkeypatch.setattr('minebot.app.body_loop.FACT_FRAME_INTERVAL_S', .001)
        loop = BodyLoop(runtime)
        await loop.start()
        await asyncio.sleep(.01)
        first = loop._lifecycle_task
        listeners[0](Event(seq=1, tick=1, bot='test', name='world_disconnected', data={}))
        await asyncio.sleep(.01)
        assert first.done()
        assert loop._lifecycle_started is False
        count = len(decision.requests)
        await loop.start()  # An Agent turn cannot reopen a disconnected world.
        result = await loop.run(None, lambda: (_ for _ in ()).throw(AssertionError('disconnected write')), ())
        assert result == {'success': False, 'reason': 'server_disconnected', 'canRetry': True}
        assert await loop._judge_terminal_frame() == 'unknown'
        await asyncio.sleep(.01)
        assert len(decision.requests) == count
        assert not any(event['event'] == 'reflex_death' for event in trace.events)
        listeners[0](Event(seq=2, tick=2, bot='test', name='world_ready', data={}))
        await asyncio.sleep(.01)
        assert loop._lifecycle_started
        assert loop._lifecycle_task is not first
        assert len(listeners) == 1
        assert len(decision.requests) > count
        await loop.stop()
        assert not listeners
        assert len([e for e in trace.events if e['event'] == 'reflex_loop_started']) == 2
        assert len([e for e in trace.events if e['event'] == 'reflex_loop_stopped']) == 2
    asyncio.run(run())


def test_immediate_reconnect_waits_for_disconnected_consumer_to_finish(monkeypatch):
    async def run():
        runtime, trace, decision = _runtime()
        listeners = []
        runtime.body = SimpleNamespace(add_lifecycle_listener=listeners.append,
                                       remove_lifecycle_listener=listeners.remove)
        monkeypatch.setattr('minebot.app.body_loop.FACT_FRAME_INTERVAL_S', .001)
        loop = BodyLoop(runtime)
        await loop.start()
        first = loop._lifecycle_task
        listeners[0](Event(seq=1, tick=1, bot='test', name='world_disconnected', data={}))
        listeners[0](Event(seq=2, tick=2, bot='test', name='world_ready', data={}))
        await asyncio.sleep(.02)
        assert first.done()
        assert loop._lifecycle_started
        assert loop._lifecycle_task is not first
        assert len(listeners) == 1
        assert decision.requests
        await loop.stop()
        await asyncio.sleep(.01)
        assert not loop._lifecycle_started
        assert not listeners
    asyncio.run(run())


def test_a_lost_and_regained_world_reaches_the_agent_once_each(monkeypatch):
    """Villa run: the client quit at 01:30; the loop stopped itself and nobody
    else heard of it for 13 hours."""
    async def run():
        runtime, trace, decision = _runtime()
        listeners = []
        notified = []
        runtime.body = SimpleNamespace(add_lifecycle_listener=listeners.append,
                                       remove_lifecycle_listener=listeners.remove)
        runtime.notify_body_lifecycle_event = notified.append
        runtime.last_known_body_state = {'position': [3, 70, 4], 'health': 18, 'inventory': {'dirt': 3}}
        monkeypatch.setattr('minebot.app.body_loop.FACT_FRAME_INTERVAL_S', .001)
        loop = BodyLoop(runtime)
        await loop.start()
        listeners[0](Event(seq=1, tick=1, bot='test', name='world_ready', data={}))
        await asyncio.sleep(.01)
        assert notified == []  # the first ready of a session is not news
        listeners[0](Event(seq=2, tick=2, bot='test', name='world_disconnected', data={'reason': 'client_connection_lost'}))
        await asyncio.sleep(.01)
        assert [event.name for event in notified] == ['world_disconnected']
        assert notified[0].data['reason'] == 'client_connection_lost'
        assert notified[0].data['last_known'] == {'position': [3, 70, 4], 'health': 18}
        listeners[0](Event(seq=3, tick=3, bot='test', name='world_ready', data={}))
        await asyncio.sleep(.01)
        assert [event.name for event in notified] == ['world_disconnected', 'world_ready']
        assert notified[1].data['disconnected_for_s'] >= 0
        assert [event['state'] for event in trace.events if event['event'] == 'body_connection'] == [
            'disconnected', 'ready']
        await loop.stop()
    asyncio.run(run())


def test_out_of_the_world_the_client_is_read_until_it_is_back(monkeypatch):
    # H6 2026-10-29: the client was back at 13:52:35 but nothing read it, so
    # the session heard world_ready at 13:53:56 (s7-later-20261029.md §8.15).
    async def run():
        runtime, trace, decision = _runtime()
        listeners, reads = [], []
        runtime.body = SimpleNamespace(add_lifecycle_listener=listeners.append,
                                       remove_lifecycle_listener=listeners.remove,
                                       get_state=lambda: reads.append(1))

        async def run_read(callback, *args, **kwargs):
            return callback(*args)

        runtime.run_read = run_read
        monkeypatch.setattr('minebot.app.body_loop.FACT_FRAME_INTERVAL_S', .001)
        monkeypatch.setattr('minebot.app.body_loop.WORLD_LOOK_INTERVAL_S', .01)
        loop = BodyLoop(runtime)
        await loop.start()
        await asyncio.sleep(.01)
        listeners[0](Event(seq=1, tick=1, bot='test', name='world_disconnected', data={}))
        await asyncio.sleep(.08)
        assert len(reads) >= 3
        listeners[0](Event(seq=2, tick=2, bot='test', name='world_ready', data={}))
        await asyncio.sleep(.05)
        assert loop._world_look_task.done()
        await loop.stop()

    asyncio.run(run())


def test_a_tool_that_finds_no_body_out_of_the_world_is_no_death():
    # H6 2026-10-29, second staged kick: a tool's missing-Body result sent the
    # session into a death recovery (s7-later-20261029.md §8.15).
    from minebot.app.runner import _out_of_world

    gone = {"success": False, "metrics": {"missing": True, "health": 0.0}}
    away = SimpleNamespace(body_loop=SimpleNamespace(world_disconnected=lambda: True))
    here = SimpleNamespace(body_loop=SimpleNamespace(world_disconnected=lambda: False))
    assert _out_of_world(away, gone)
    assert not _out_of_world(here, gone)
    assert not _out_of_world(away, {"success": False, "metrics": {"missing": False, "health": 0.0}})
