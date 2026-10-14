"""Undeployed candidates through the online compiler, using captured raw facts."""
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
exec(compile((root/'logs/specific-report-and-observation-trial.py').read_text().split('\nasync def main():')[0], __file__, 'exec'))
import subprocess
import types
old_question = types.ModuleType('baseline_reflex_question')
exec(compile(subprocess.check_output(['git','show','b3ac247:minebot/app/reflex_question.py'],cwd=root,text=True),
             'b3ac247:minebot/app/reflex_question.py','exec'),old_question.__dict__)
original_options, original_result = old_question.frame_options, old_question.result_sentence

def candidate_options(**kwargs):
    options = original_options(**kwargs)
    if 'next_step' in options and not kwargs.get('next_call') and kwargs.get('intent'):
        stop = 'Stop the current action' if kwargs['running'] else 'Do not repeat it'
        options['next_step'] = f'{stop} and report to the Agent that “{kwargs["intent"]}” has been achieved.'
    return options

def candidate_result(call, result):
    sentence = original_result(call, result)
    if isinstance(result, dict) and result.get('success') is not True:
        for field in ('summary', 'metrics'):
            details = result.get(field)
            if isinstance(details, dict) and isinstance(details.get('entities_observed'), int) and isinstance(details.get('search_radius'), (int,float)):
                return sentence + f' The tool observed {details["entities_observed"]} entities within {details["search_radius"]:g} blocks.'
    return sentence

def compile_fixture(case):
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace(), trace=SimpleNamespace(emit=lambda *a,**k:None),
        agent_context=SimpleNamespace(goal_text=case['goal'])))
    raw = case['frame_input']; loop._vitals._samples = copy.deepcopy(raw['vital_samples'])
    with patch('time.monotonic',return_value=raw['monotonic_s']): frame = loop._frame(raw['execution'])
    tool = SimpleNamespace(name=case['tool'])
    record = {'tool':case['tool'],'params':case['params'],'supervision_context':case['supervision_context']}
    options = loop._options_for(tool,record,execution=frame)
    request = loop._question(case['call_id'],tool,case['params'],frame,idle_s=case['idle_s'],purpose=case['purpose'],
        supervision_context=case['supervision_context'],options=options)
    request.pop('_request_chars',None); request.pop('_request_chars_before',None)
    return request

def compile_plan(case):
    return WorkPlanExecutor(case['plan'],call=None)._call_question(case['node'],case['node_id'],case['intents'],
        NodeRecord(decision_facts=copy.deepcopy(case['raw_decision_facts'])))

async def main():
    output=root/'logs/expanded-report-observation-trial-b3ac247.jsonl'
    cases=[]
    for name,seq,expected,running in [('player-handler-fixed',333,'keep_going',True),('player-handler-fixed',862,'keep_going',True),('life-30-final-v2',4863,'next_step',False)]:
        rows=[json.loads(line) for line in (root/f'logs/reflex-always-on-20261014-{name}/trace.jsonl').read_text().splitlines()]
        row=next(r for r in rows if r['seq']==seq)
        baseline=compile_online(row) if running else compile_terminal(row)
        recorded=next(r['choice'] for r in rows if r['seq']>seq and r['event']=='call_supervision_result' and r.get('tool_call_id')==row['tool_call_id'] and r.get('purpose')==row['purpose'])
        current=copy.deepcopy(baseline)
        options=candidate_options(tool=row['tool'],params=row['compiler_input']['params'],intent=row['compiler_input']['supervision_context']['intent'],facts={},running=running,result=row['compiler_input']['decision_context'].get('tool_result'),next_call=row['compiler_input']['supervision_context'].get('next_call',''))
        current['questions']['q']['criteria']['next_step']=options['next_step']
        cases.append((f'{name}:{seq}',baseline,current,expected,recorded))
    for case in json.loads((root/'tests/fixtures/reflex_step2_running_intent_frames.json').read_text())['cases']:
        baseline=compile_fixture(case)
        with patch.object(rq,'frame_options',candidate_options): current=compile_fixture(case)
        cases.append((f'running:{case["seq"]}',baseline,current,case['expected_choice'],None))
    for case in json.loads((root/'tests/fixtures/reflex_step2_plan_result_frames.json').read_text())['cases']:
        baseline=compile_plan(case)
        with patch.object(rq,'frame_options',candidate_options),patch.object(rq,'result_sentence',candidate_result): current=compile_plan(case)
        cases.append((f'plan_result:{case["seq"]}',baseline,current,None,None))
    for name,expected in [('broken-tool-frame-v3','equip:stone_sword'),('unrelated-frame','call_agent')]:
        frame=json.loads((root/f'logs/reflex-always-on-20261014c-step6-native/{name}.json').read_text())
        node={'kind':'call','id':'fight','tool':frame['tool'],'arguments':frame['arguments'],'intent':frame['intent']}
        case={'node':node,'node_id':'fight','intents':[frame['intent']], 'plan':{'language':'minebot-work/0.2','goal':frame['intent'],'intent':frame['intent'],'timeout_s':30,'work':node},
            'raw_decision_facts':{'tool_result':_model_tool_payload(frame['tool'],frame['result'],trace_ref=name),'tool_arguments':frame['arguments'],'body_state':frame['facts']}}
        baseline=compile_plan(case)
        previous=[json.loads(line) for line in (root/'logs/r3-negative-current-replay-0a2840b.jsonl').read_text().splitlines()]
        old=next(r for r in previous if r['name']==name and r['variant']=='baseline')
        opts=baseline['questions']['q']['criteria']; baseline['questions']['q']['criteria']={k:opts[k] for k in old['request']['questions']['q']['criteria']}
        assert baseline==old['request']
        with patch.object(rq,'frame_options',candidate_options),patch.object(rq,'result_sentence',candidate_result):current=compile_plan(case)
        cases.append((name,baseline,current,expected,old['choice']))
    client=DecisionClient(); report=[]
    try:
        with output.open('x') as sink:
            for name,baseline,current,expected,recorded in cases:
                answer=await client.ask(baseline); selected=rq.frame_choice(answer.raw,baseline['questions'])
                item={'case':name,'expected':expected,'recorded':recorded,'baseline':selected,'changed':baseline!=current}
                report.append(item)
                sink.write(json.dumps({'case':name,'variant':'baseline','request':baseline,'choice':selected,'response':answer.raw},ensure_ascii=False)+'\n');sink.flush()
                if recorded is not None and selected!=recorded:
                    item['status']='baseline_not_reproduced';print(json.dumps(item),flush=True);break
                names=list(current['questions']); choices=[list(itertools.permutations(current['questions'][n]['criteria'])) for n in names]
                orders=list(itertools.islice(itertools.product(*choices),12));unique=len(orders)
                orders=(orders*12)[:12];counts=Counter()
                for order in orders:
                    request=copy.deepcopy(current)
                    for n,keys in zip(names,order):request['questions'][n]['criteria']={k:current['questions'][n]['criteria'][k] for k in keys}
                    answer=await client.ask(request);choice=rq.frame_choice(answer.raw,request['questions']);counts[choice]+=1
                    sink.write(json.dumps({'case':name,'variant':'candidate','request':request,'choice':choice,'response':answer.raw,'elapsed_s':answer.elapsed_s},ensure_ascii=False)+'\n');sink.flush()
                item.update(distribution=dict(counts),unique_orders=unique,hits=counts[expected] if expected else None)
                print(json.dumps(item,ensure_ascii=False),flush=True)
    finally:
        await client.close();output.with_suffix('.summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
with patch.object(rq,'frame_options',original_options),patch.object(rq,'result_sentence',original_result):
    asyncio.run(main())
