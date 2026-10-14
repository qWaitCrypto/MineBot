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
