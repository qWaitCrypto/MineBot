"""Draft regressions from captured native facts; not real-Agent acceptance."""
import asyncio
import copy
from pathlib import Path
import sys

sys.path.insert(0, '/home/qwait/MineBot-always-on')
import pytest
from minebot.app import reflex_question as rq
from minebot.app.work_runtime import WorkPlanExecutor


@pytest.mark.parametrize('key', ['entities', 'nearby_entities', 'entities_near'])
def test_native_player_list_feeds_prose_options_and_binding(key):
    player = {'id': '47adfe28-5d0f-31e3-992c-e409ace53612', 'type': 'player',
              'name': 'Visitor', 'dist2': 36, 'pos': [256.5, 121, 250.5]}
    facts = {key: [player], 'position': [250.5, 121, 250.5],
             'life_profile': {'relationships': [{'player': 'Visitor'}]}}
    assert rq.nearby_players(facts) == [player]
    options = rq.idle_options(facts)
    assert {'look_player', 'approach_player', 'follow_player'} <= options.keys()
    assert 'seek_player' not in options
    assert rq.idle_question(facts, options)['state']['players'] == 'Player Visitor is 6 blocks away.'


def test_explicit_empty_nearby_player_sample_does_not_revive_stale_native_players():
    assert rq.nearby_players({'nearby_entities': [], 'entities': [{'type': 'player'}]}) == []


@pytest.mark.parametrize('shape', ['call', 'sequence', 'choice', 'batch', 'adaptive_batch'])
def test_handler_terminal_has_suspended_call_and_still_requires_resume_decision(shape):
    async def run():
        leaf = {'kind': 'call', 'id': 'equip', 'tool': 'equip_item',
                'arguments': {'item': 'stone_sword', 'slot': 'mainhand'},
                'intent': 'Equip the stone sword, then continue the patrol'}
        handler = copy.deepcopy(leaf)
        if shape == 'sequence':
            handler = {'kind': 'sequence', 'steps': [copy.deepcopy(leaf), {**leaf, 'id': 'second'}]}
        elif shape == 'choice':
            handler = {'kind': 'choice', 'options': [{'id': 'equip', 'intent': leaf['intent'], 'work': leaf}]}
        elif shape in {'batch', 'adaptive_batch'}:
            handler = {'kind': 'batch', 'items': ['first', 'last'], 'body': leaf}
            if shape == 'adaptive_batch':
                handler['order'] = 'adaptive'
        handler.setdefault('intent', leaf['intent'])
        plan = {'language': 'minebot-work/0.2', 'goal': 'Complete the patrol', 'intent': 'Complete the patrol',
                'timeout_s': 10, 'work': {'kind': 'call', 'id': 'original', 'tool': 'move_to',
                'arguments': {'pos': [190, 121, 220]}, 'intent': 'Reach the next patrol point'},
                'interrupt_when': {'condition': {'text': 'The hand is empty and a sword is available'}, 'handler': handler}}
        contexts = []
        questions = []
        resume_answers = []
        executor = None
        async def call(tool, args, *, call_id, timeout_s):
            contexts.append((tool, executor.call_context(call_id)))
            if len(contexts) == 1:
                return {'success': False, 'reason': 'preempted', '_supervision_handoff': {
                    'facts': {'decision': {'choice': 'interrupt'}}}}
            return {'success': True, 'reason': 'equipped' if tool == 'equip_item' else 'arrived'}
        async def state():
            return {'position': [220, 121, 220], 'health': 20, 'selected_item': 'stone_sword'}
        async def judge(**kw):
            questions.append(kw['question'])
            if kw['tool'].name == 'interrupt_handler':
                resume_answers.append('resume')
                return 'resume'
            return 'next_step'
        class Choice:
            async def ask(self, request, **kw):
                return next(iter(request['questions']['q']['criteria']))
        executor = WorkPlanExecutor(plan, call=call, state=state, result_judge=judge, decision=Choice())
        assert (await executor.run()).status == 'completed'
        handler_contexts = [ctx for tool, ctx in contexts if tool == 'equip_item']
        assert handler_contexts[-1]['next_call'] == 'move_to(pos=[190, 121, 220])'
        assert all('interrupt_when' not in ctx for ctx in handler_contexts)
        assert resume_answers == ['resume']
        assert contexts[-1][0] == 'move_to'
        assert any('start the next step of the plan: move_to(pos=[190, 121, 220])' in
                   question['questions']['q']['criteria'].get('next_step', '') for question in questions)
    asyncio.run(run())
