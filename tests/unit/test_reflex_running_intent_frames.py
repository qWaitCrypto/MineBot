"""Restored real inputs, verified offline before the running-only wording change."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app import reflex_question as rq
from tests.unit.reflex_expected import intent_question_while_running, main_hand_beside_inventory


def test_real_running_inputs_compile_to_the_exact_offline_verified_requests():
    fixture=json.loads((Path(__file__).parents[1]/'fixtures/reflex_step2_running_intent_frames.json').read_text())
    assert len(fixture['cases']) == 15
    assert sum(case['expected_choice']=='keep_going' for case in fixture['cases']) == 13
    for case in fixture['cases']:
        compiler=CallSupervisor(SimpleNamespace(
            trace=SimpleNamespace(emit=lambda *a,**k:None),
            agent_context=SimpleNamespace(goal_text=case['goal'])))
        saved=case['frame_input']
        compiler._vitals._samples=copy.deepcopy(saved['vital_samples'])
        with patch('time.monotonic',return_value=saved['monotonic_s']):
            execution=compiler._frame(saved['execution'])
        tool=SimpleNamespace(name=case['tool'])
        options=compiler._options_for(tool,{'tool':tool.name,'params':case['params'],
            'supervision_context':case['supervision_context']},execution=execution)
        request=compiler._question(case['call_id'],tool,case['params'],execution,
            idle_s=case['idle_s'],purpose=case['purpose'],options=options,
            supervision_context=case['supervision_context'])
        request.pop('_request_chars',None);request.pop('_request_chars_before',None);done=request.pop('_done',None)
        # Keep the historical fixture intact. The only new fact is the
        # recorded main hand, omitted by the original sentence compiler.
        expected=main_hand_beside_inventory(case['expected_request'],execution.get('facts',{}))
        # Whether the intent is shown is now its own question while running;
        # protective calls are not asked it. All facts stay as recorded.
        context=case['supervision_context']
        if context.get('intent') and not context.get('is_reflex_action'):
            expected,expected_done=intent_question_while_running(expected,context['intent'])
            assert done==expected_done,case['seq']
        else:
            assert done is None,case['seq']
        assert request==expected,case['seq']
        assert json.dumps(request,ensure_ascii=False,separators=(',',':')) == json.dumps(
            expected,ensure_ascii=False,separators=(',',':')),case['seq']
        if case['supervision_context'].get('is_reflex_action'):
            assert 'next_step' not in request['questions']['q']['criteria']


def test_terminal_and_idle_wording_is_not_changed_by_running_intent_clarification():
    state=rq.frame_state(goal='make a pickaxe',intent='there is a pickaxe in the inventory',
        tool='craft_item',params={'item':'wooden_pickaxe'},facts={'inventory':{'wooden_pickaxe':1}},
        running=False,result={'success':True})
    assert state['agent_intent']=='there is a pickaxe in the inventory'
    assert 'current_work' not in state
    assert rq.frame_questions({'next_step':'start the next call'})['q']['instructions']=='Choose what the Body does next.'
    idle=rq.idle_question({'health':20,'oxygen':300},rq.idle_options({'health':20,'oxygen':300}))
    assert 'agent_intent' not in idle['state']
    assert idle['questions']['q']['instructions']=='Choose what the Body does next.'
