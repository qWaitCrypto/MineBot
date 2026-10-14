"""Declared interruption execution and cancellation, not semantic acceptance."""
import asyncio
import copy
import time
from types import SimpleNamespace
from minebot.app import reflex_question as rq
from minebot.app.body_loop import BodyLoop
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app.work_runtime import WorkPlanExecutor


def plan():
    return {"language": "minebot-work/0.2", "goal": "Reach the far point", "timeout_s": 10,
            "work": {"kind": "call", "id": "original", "intent": "Reach [20,64,0]", "tool": "move_to", "arguments": {"x":20}},
            "interrupt_when": {"condition": {"text": "A visitor is nearby"},
                "handler": {"kind": "call", "id": "visit", "intent": "Stand by the visitor", "tool": "move_to",
                            "arguments": {"x":5,"observation":{"id":"entity-id"}}}}}


def test_declared_interrupt_in_running_options_but_not_in_handler_context():
    p=plan()
    executor=WorkPlanExecutor(p,call=lambda *a,**k:None)
    context=executor._call_plan(p['work'],[])
    record={'tool':'move_to','params':{'x':20},'supervision_context':context}
    options=CallSupervisor._options_for(SimpleNamespace(name='move_to'),record,execution={'facts':{},'state':'running'})
    assert options['interrupt']=='Stop the current action because “A visitor is nearby” and do: Stand by the visitor.'
    executor._handling_interrupt=True
    assert 'interrupt_when' not in executor._call_plan(p['interrupt_when']['handler'],[])


def test_handler_then_decision_resume_reenters_original_without_agent_judgement():
    async def run():
        calls=[];events=[]
        executor=None
        async def call(tool,arguments,*,call_id,timeout_s):
            context=executor.call_context(call_id)
            calls.append((call_id,copy.deepcopy(arguments),context))
            if len(calls)==1:
                return {'success':False,'reason':'preempted','_supervision_handoff':{
                    'facts':{'decision':{'choice':'interrupt'}}}}
            return {'success':True,'reason':'arrived'}
        async def state():return {'position':[5,64,0],'health':20,'inventory':{}}
        async def judge(**kw):
            if kw['tool'].name=='interrupt_handler':return 'resume'
            return 'next_step'
        executor=WorkPlanExecutor(plan(),call=call,state=state,result_judge=judge,
            emit=lambda name,**kw:events.append((name,kw)))
        result=await executor.run()
        assert result.status=='completed'
        assert [c[0] for c in calls]==['original','original__interrupt_0__visit','original__repeat_1']
        assert 'interrupt_when' not in calls[1][2]
        assert 'interrupt_when' in calls[2][2]
        assert calls[1][1]['observation']['id']=='entity-id'
        assert sum(name=='work_interrupt_result' for name,_ in events)==1
    asyncio.run(run())


def test_unknown_after_handler_keeps_terminal_without_reexecuting_it_and_times_out():
    async def run():
        calls=[]
        async def call(tool,args,**kw):
            calls.append(kw['call_id'])
            if len(calls)==1:return {'success':False,'reason':'preempted','_supervision_handoff':{'facts':{'decision':{'choice':'interrupt'}}}}
            return {'success':True,'reason':'arrived'}
        async def state():return {'position':[5,64,0],'health':20}
        async def judge(**kw):
            if kw['tool'].name=='interrupt_handler':raise ConnectionError('service down')
            return 'next_step'
        executor=WorkPlanExecutor(plan(),call=call,state=state,result_judge=judge)
        assert (await executor.run()).status=='waiting'
        assert (await executor.run()).status=='waiting'
        assert len(calls)==2
        executor._deadline=0
        assert (await executor.run()).status=='blocked'
        assert len(calls)==2
    asyncio.run(run())


def test_user_cancel_during_handler_never_reenters_original():
    async def run():
        calls=[];started=asyncio.Event()
        async def call(tool,args,**kw):
            calls.append(kw['call_id'])
            if len(calls)==1:return {'success':False,'reason':'preempted','_supervision_handoff':{'facts':{'decision':{'choice':'interrupt'}}}}
            started.set();await asyncio.Event().wait()
        async def state():return {}
        executor=WorkPlanExecutor(plan(),call=call,state=state)
        task=asyncio.create_task(executor.run())
        await started.wait();task.cancel()
        assert (await task).status=='cancelled'
        assert len(calls)==2
    asyncio.run(run())


def test_interrupt_stop_preserves_plan_generation_and_does_not_notify_agent():
    async def run():
        steps=[];notifications=[]
        async def cancel(reason,**kw):
            steps.append((reason,kw));return {'settled':True}
        owner=SimpleNamespace(decision_client=SimpleNamespace(),direct_call_handoffs={},
            cancel_active_execution_with_facts=cancel,notify_reflex_fact=notifications.append,
            trace=SimpleNamespace(emit=lambda *a,**k:None))
        body=BodyLoop(owner);body.loop_state='work'
        record={'call_id':'plan-original','cancel_requested':False,'supervision_context':{'is_plan':True}}
        body.current=record
        await body._handoff(record,{'tool':'move_to','facts':{'position':[0,64,0]}},
                            {'reason':'decision_interrupt','confirmed':False},decision='interrupt')
        assert steps[0][1]['invalidate_generation'] is False
        assert notifications==[]
        assert owner.direct_call_handoffs['plan-original']['facts']['decision']['choice']=='interrupt'
    asyncio.run(run())


def test_equipment_is_only_a_failed_result_option_for_an_owned_other_tool_of_same_category():
    body={'selected_item':'wooden_pickaxe','inventory':{'stone_pickaxe':1,'iron_sword':1,'iron_pickaxe':0}}
    failed={'success':False,'reason':'target_unreachable'}
    options=rq.frame_options(tool='collect_resource',params={'item':'stone'},intent='Collect stone',facts=body,running=False,result=failed)
    assert options['equip:stone_pickaxe']=='Equip stone_pickaxe from the inventory and run the same call again: collect_resource(item="stone").'
    assert not {k for k in rq.frame_options(tool='collect_resource',params={},intent='Collect stone',facts=body,running=True) if k.startswith('equip:')}
    body['selected_item']=''
    failed['summary']={'broken_item':'wooden_pickaxe'}
    assert rq.equipment_candidates(body,failed)=={'equip:stone_pickaxe':'stone_pickaxe'}
    body['selected_item']='stone_pickaxe'
    assert rq.equipment_candidates(body,failed)=={}


def test_failed_protection_result_offers_same_owned_equipment_as_work_result():
    facts={'selected_item':'','inventory':{'stone_sword':1}}
    result={'success':False,'reason':'tool_broke','canRetry':False,
            'metrics':{'broken_item':'wooden_sword'}}
    options=rq.after_default_options(original_call='engage_entity(target="id")',
        intent='Kill the target',default_call='engage_entity(target="id")',result=result,facts=facts)
    assert list(options)==['resume','equip:stone_sword','call_agent']
    assert options['equip:stone_sword']=='Equip stone_sword from the inventory and run the same call again: engage_entity(target="id").'
    result['success']=True
    assert not any(k.startswith('equip:') for k in rq.after_default_options(
        original_call='engage_entity(target="id")',intent='Kill the target',
        default_call='engage_entity(target="id")',result=result,facts=facts))


def test_protection_equipment_is_judged_then_retries_protection_without_agent_wake():
    async def run():
        calls=[];retries=[];questions=[];notifications=[];stops=[]
        facts={'health':20,'selected_item':'','inventory':{'stone_sword':1}}
        async def state():return copy.deepcopy(facts)
        async def cancel(reason,**kw):stops.append((reason,kw));return {'settled':True}
        async def call(tool,*args,**kw):
            calls.append(kw)
            assert kw['claim_holder']=='threat'
            assert kw['supervision_context']['is_reflex_action'] is True
            facts['selected_item']='stone_sword'
            return {'success':True,'reason':'equipped','_execution':{'delivered':True}}
        owner=SimpleNamespace(decision_client=SimpleNamespace(),
            registry=SimpleNamespace(get=lambda name:SimpleNamespace(name=name)),
            run_tool=call,cancel_active_execution_with_facts=cancel,
            notify_reflex_fact=notifications.append,trace=SimpleNamespace(emit=lambda *a,**kw:None))
        loop=BodyLoop(owner)
        loop._read_idle_facts=state
        async def judge(*args,**kw):
            questions.append(kw['options'])
            if len(questions)==1:
                assert 'equip:stone_sword' in kw['options']
                return 'equip:stone_sword'
            return 'resume'
        loop._judge_terminal_frame=judge
        async def equip_judge(**kw):
            assert kw['tool'].name=='equip_item'
            assert facts['selected_item']=='stone_sword'
            return 'next_step'
        loop.decide_plan_result=equip_judge
        async def retry(choice,action,record,**kw):
            assert facts['selected_item']=='stone_sword'
            retries.append(action)
            return choice,action,{'success':True,'reason':'killed'},kw['call_id']
        loop._execute_protection=retry
        record={'call_id':'original','tool':'engage_entity','params':{'target':'id'},
            'started_at':time.monotonic(),'timeout_s':2,'questions':0,
            'supervision_context':{'intent':'Kill the target'}}
        failed={'success':False,'reason':'tool_broke','canRetry':False,'metrics':{'broken_item':'wooden_sword'}}
        answer=await loop._after_default_action(record,('engage_entity',{'target':'id'}),failed,'fight',protective_choice='engage')
        assert answer=='resume'
        assert len(calls)==1 and len(questions)==2
        assert retries==[('engage_entity',{'target':'id'})]
        assert all(not kwargs['invalidate_generation'] for _,kwargs in stops)
        assert not notifications
        assert not loop._result_pending
    asyncio.run(run())


def test_equipment_node_is_judged_before_original_call_reentry():
    async def run():
        calls=[];judged=[];body={'selected_item':'','inventory':{'stone_sword':1}}
        async def state():return copy.deepcopy(body)
        async def call(tool,args,**kw):
            calls.append((tool,copy.deepcopy(args)))
            if len(calls)==1:return {'success':False,'reason':'tool_broke','canRetry':False,'summary':{'broken_item':'wooden_sword'}}
            if tool=='equip_item':body['selected_item']=args['item']
            return {'success':True,'reason':'completed'}
        async def judge(**kw):
            judged.append(kw['tool'].name)
            if len(judged)==1:return 'equip:stone_sword'
            return 'next_step'
        p={'language':'minebot-work/0.2','goal':'Kill the zombie','timeout_s':5,
           'work':{'kind':'call','id':'fight','intent':'Kill the zombie','tool':'engage_entity','arguments':{'target':'observed-uuid'}}}
        result=await WorkPlanExecutor(p,call=call,state=state,result_judge=judge).run()
        assert result.status=='completed'
        assert calls==[('engage_entity',{'target':'observed-uuid'}),('equip_item',{'item':'stone_sword','target':'mainhand'}),('engage_entity',{'target':'observed-uuid'})]
        assert judged==['engage_entity','equip_item','engage_entity']
    asyncio.run(run())


def test_handler_call_inherits_root_intent_without_extra_required_field():
    p = plan()
    del p['interrupt_when']['handler']['intent']
    executor = WorkPlanExecutor(p, call=None)
    context = executor._call_plan(p['work'], [p['work']['intent']])
    assert context['interrupt_when']['handler']['intent'] == p['work']['intent']


def test_direct_equipment_unknown_waits_without_replaying_the_write(monkeypatch):
    async def run():
        calls=[];stops=[];judged=[]
        async def cancel(reason, **kw): stops.append((reason, kw))
        async def tool_call(tool, *args, **kw):
            calls.append(kw)
            return {'success': True, 'reason': 'equipped', '_execution': {'delivered': True}}
        runtime = SimpleNamespace(decision_client=SimpleNamespace(),
            registry=SimpleNamespace(get=lambda name:SimpleNamespace(name=name)),
            cancel_active_execution_with_facts=cancel, run_tool=tool_call,
            trace=SimpleNamespace(emit=lambda *a, **kw: None))
        loop = BodyLoop(runtime)
        async def state(): return {'selected_item': 'stone_sword', 'inventory': {'stone_sword':1}}
        async def judge(**kw):
            judged.append(kw)
            return 'unknown' if len(judged)==1 else 'next_step'
        loop._read_idle_facts = state
        loop.decide_plan_result = judge
        record = {'started_at':time.monotonic(), 'timeout_s':2, 'supervision_context':{'intent':'Kill the zombie'}}
        answer = await loop._run_result_equipment(record, SimpleNamespace(name='engage_entity'), {'target':'id'}, 'stone_sword')
        assert answer == 'repeat'
        assert len(calls)==1 and len(judged)==2
        assert calls[0]['tool_call_id'].startswith('plan-equipment_')
        assert stops[0][1]['invalidate_generation'] is False
        assert record['equipment_result']['result']
    monkeypatch.setattr('minebot.app.direct_call_supervision.FACT_FRAME_INTERVAL_S', .001)
    asyncio.run(run())


def test_equipment_respects_original_node_deadline_without_starting_write():
    async def run():
        from minebot.app.work_runtime import NodeRecord
        p=plan();p['work']['timeout_s']=.01
        executor=WorkPlanExecutor(p,call=None)
        record=NodeRecord(started_at=time.monotonic()-1,interruption={'kind':'equipment','item':'stone_sword'})
        assert await executor._handle_equipment(p['work'],'original','work',[],record)=='blocked'
        assert record.blocked['reason']=='node_timeout'
    asyncio.run(run())
