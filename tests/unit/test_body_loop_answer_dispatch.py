"""Running answers bind the facts they were asked about; not model acceptance."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from minebot.app.body_loop import BodyLoop


def setup_loop():
    events = []
    native = {'operation_id': 'life', 'state': 'running', 'phase': 'following',
              'fact_revision': 1, 'facts': {'position': [10, 64, 0], 'health': 20,
                                         'food': 16, 'oxygen': 300}}
    runtime = SimpleNamespace(trace=SimpleNamespace(emit=lambda event, **kw: events.append((event, kw))),
        decision_client=SimpleNamespace(), direct_call_handoffs={},
        execution_snapshots=SimpleNamespace(current=lambda: copy.deepcopy(native), last=lambda: None,
                                             tick=lambda call_id: None))
    loop = BodyLoop(runtime)
    loop._life_profile = {'home': {'pos': [0, 64, 0]}, 'routine': 'Go home at dusk.'}
    loop._ambient_facts = {'inventory': {'apple': 1}, 'nearby_entities': [
        {'type': 'player', 'name': 'Visitor', 'id': 'player-1', 'pos': [12, 64, 0]},
        {'type': 'zombie', 'hostile': True, 'id': 'hostile-1', 'pos': [11, 64, 0], 'dist': 1},
    ]}
    record = {'call_id': 'life', 'tool': 'follow_entity', 'params': {'target': 'player-1'},
              'cancel_requested': False, 'questions': 0, 'reads': 0, 'unknown_answers': 0,
              'initial_reader_done': True, 'supervision_context': {
                  'is_life_action': True, 'is_reflex_action': True,
                  'reflex_action_choice': 'follow_player'}}
    loop.current = record
    return loop, native, record, events


@pytest.mark.parametrize(('choice', 'tool', 'expected'), [
    ('go_home', 'move_to', {'pos': [0, 64, 0]}),
    ('look_player', '__reflex_look_at', {'pos': [12, 64, 0]}),
    ('eat', 'consume_item', {'item': 'apple'}),
    ('move_away', 'move_away', {'danger_pos': [11.0, 64.0, 0.0]}),
])
def test_running_action_uses_the_full_asked_frame_even_when_native_target_changes(choice, tool, expected):
    async def run():
        loop, native, record, events = setup_loop()
        finish = asyncio.Event()
        worker = asyncio.create_task(finish.wait())
        wake = asyncio.Event()
        wake.set()

        async def judge(call_id, target, params, frame, idle_s, **kwargs):
            assert choice in kwargs['options']
            assert frame['facts']['life_profile']['home']['pos'] == [0, 64, 0]
            # An updated native sample does not retarget the earlier answer.
            native['facts']['nearby_entities'] = [
                {'type': 'player', 'name': 'Other', 'id': 'player-2', 'pos': [90, 64, 0]}]
            return choice

        async def handoff(*args, **kwargs):
            record['cancel_requested'] = True

        loop.judge = judge
        loop._handoff = handoff
        try:
            await asyncio.wait_for(loop._monitor(record, SimpleNamespace(name='follow_entity'),
                                                record['params'], wake, worker), .5)
            selected, action = loop._protective_redirect
            assert selected == choice and action[0] == tool
            assert all(action[1][key] == value for key, value in expected.items())
            assert not any(event == 'call_supervision_default_unknown' for event, _ in events)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_tool_terminal_discards_a_running_stop_answer_without_handoff():
    async def run():
        loop, native, record, events = setup_loop()
        finish = asyncio.Event()
        worker = asyncio.create_task(finish.wait())
        wake = asyncio.Event()
        wake.set()
        handoffs = []

        async def judge(*args, **kwargs):
            native['state'] = 'succeeded'
            return 'call_agent'

        async def handoff(*args, **kwargs):
            handoffs.append(True)

        loop.judge = judge
        loop._handoff = handoff
        try:
            await asyncio.wait_for(loop._monitor(record, SimpleNamespace(name='follow_entity'),
                                                record['params'], wake, worker), .5)
            assert handoffs == [] and record['cancel_requested'] is False
            assert any(event == 'call_supervision_answer_discarded' for event, _ in events)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_tool_terminal_cancels_pending_running_question_without_waiting_for_service():
    async def run():
        loop, _, record, _ = setup_loop()
        finish, entered, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        worker = asyncio.create_task(finish.wait())
        wake = asyncio.Event()
        wake.set()

        async def judge(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        loop.judge = judge
        job = asyncio.create_task(loop._monitor(record, SimpleNamespace(name='follow_entity'),
                                                record['params'], wake, worker))
        try:
            await asyncio.wait_for(entered.wait(), .5)
            finish.set()
            await asyncio.wait_for(job, .2)
            assert cancelled.is_set()
            assert record['cancel_requested'] is False
        finally:
            job.cancel()
            worker.cancel()
            await asyncio.gather(job, worker, return_exceptions=True)
    asyncio.run(run())
