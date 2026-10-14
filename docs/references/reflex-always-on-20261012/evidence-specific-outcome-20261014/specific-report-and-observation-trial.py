"""Undeployed semantic candidates from captured raw facts, not new policies."""
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
prefix = (root / 'logs/interrupt-condition-trial.py').read_text().split('async def main():')[0]
exec(compile(prefix, __file__, 'exec'))
from minebot.app.runner import _model_tool_payload
from minebot.app.work_runtime import WorkPlanExecutor, NodeRecord

def compile_terminal(row):
    ci, raw = row['compiler_input'], row['frame_input']
    # Task is metadata, not reconstructed Body prose. This direct call has
    # no separate plan goal in its recorded compiler context.
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace(),
        agent_context=SimpleNamespace(goal_text=row['request']['state']['task']),
        trace=SimpleNamespace(emit=lambda *a, **k: None)))
    loop._vitals._samples = copy.deepcopy(raw['vital_samples'])
    loop._ambient_facts = copy.deepcopy(raw.get('ambient_facts', {}))
    loop.safe_standing_position = copy.deepcopy(raw.get('safe_standing_position'))
    with patch('time.monotonic', return_value=raw['monotonic_s']):
        frame = loop._frame(raw['execution'])
    options = rq.frame_options(tool=row['tool'], params=ci['params'],
        intent=ci['supervision_context']['intent'], facts=ci['decision_context']['body_state'],
        running=False, result=ci['decision_context']['tool_result'])
    result = loop._question(row['tool_call_id'], SimpleNamespace(name=row['tool']), ci['params'], frame,
        idle_s=row['idle_s'], purpose=row['purpose'], supervision_context=ci['supervision_context'],
        options=options, decision_context=ci['decision_context'])
    result.pop('_request_chars', None); result.pop('_request_chars_before', None)
    assert result == row['request']
    return result

async def main():
    output = root / 'logs/specific-report-and-observation-trial-b3ac247.jsonl'
    client = DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    reports = []
    async def ask_orders(sink, case, variants, expected, recorded):
        original = variants['baseline']
        answer = await client.ask(original)
        chosen = rq.frame_choice(answer.raw, original['questions'])
        item = {'case':case, 'recorded':recorded, 'baseline':chosen, 'expected':expected, 'candidate_only':True}
        reports.append(item)
        sink.write(json.dumps({'case':case, 'variant':'baseline', 'request':original,
            'response':answer.raw, 'choice':chosen}, ensure_ascii=False)+'\n'); sink.flush()
        if chosen != recorded:
            item['status']='baseline_not_reproduced; no comparison'
            return
        item['variants']={}
        for label, request in variants.items():
            if label == 'baseline': continue
            options=request['questions']['q']['criteria']
            counts=Counter()
            for order in (list(itertools.permutations(options))*12)[:12]:
                current=copy.deepcopy(request)
                current['questions']['q']['criteria']={key:options[key] for key in order}
                answer=await client.ask(current)
                selected=rq.frame_choice(answer.raw,current['questions']); counts[selected]+=1
                sink.write(json.dumps({'case':case,'variant':label,'request':current,'response':answer.raw,
                    'choice':selected},ensure_ascii=False)+'\n');sink.flush()
            item['variants'][label]=dict(counts)
        print(json.dumps(item,ensure_ascii=False),flush=True)
    try:
        with output.open('x') as sink:
            for run_name, seq, expected, running in (
                ('player-handler-fixed',333,'keep_going',True),
                ('player-handler-fixed',862,'keep_going',True),
                ('life-30-final-v2',4863,'next_step',False)):
                rows=[json.loads(line) for line in (root/f'logs/reflex-always-on-20261014-{run_name}/trace.jsonl').read_text().splitlines()]
                row=next(r for r in rows if r['seq']==seq)
                request=compile_online(row) if running else compile_terminal(row)
                old=next(r['choice'] for r in rows if r['seq']>seq and r['event']=='call_supervision_result'
                    and r.get('tool_call_id')==row['tool_call_id'] and r.get('purpose')==row['purpose'])
                variants={'baseline':request}
                # The internal step already names the following call and is a
                # control frame: preserve it. Only vague terminal reporting changes.
                if not row['compiler_input']['supervision_context'].get('next_call'):
                    for label in ('report_intent','report_call'):
                        current=copy.deepcopy(request)
                        stop='Stop the current action' if running else 'Do not repeat it'
                        intent=row['compiler_input']['supervision_context']['intent']
                        call=rq.call_text(row['tool'],row['compiler_input']['params'])
                        current['questions']['q']['criteria']['next_step']=(
                            f'{stop} and report to the Agent that “{intent}” has been achieved.'
                            if label=='report_intent' else f'{stop} and report to the Agent that {call} is finished.')
                        variants[label]=current
                else:
                    variants['unchanged_control']=copy.deepcopy(request)
                await ask_orders(sink,f'{run_name}:{seq}',variants,expected,old)
            frame=json.loads((root/'logs/reflex-always-on-20261014c-step6-native/unrelated-frame.json').read_text())
            projected=_model_tool_payload(frame['tool'],frame['result'],trace_ref='unrelated-frame')
            node={'kind':'call','id':'fight','tool':frame['tool'],'arguments':frame['arguments'],'intent':frame['intent']}
            plan={'language':'minebot-work/0.2','goal':frame['intent'],'intent':frame['intent'],'timeout_s':30,'work':node}
            executor=WorkPlanExecutor(plan,call=None)
            record=NodeRecord(decision_facts={'tool_result':projected,'tool_arguments':frame['arguments'],'body_state':frame['facts']})
            request=executor._call_question(node,'fight',[frame['intent']],record)
            previous=[json.loads(line) for line in (root/'logs/r3-negative-current-replay-0a2840b.jsonl').read_text().splitlines()]
            old=next(r for r in previous if r['name']=='unrelated-frame' and r['variant']=='baseline')
            baseline=copy.deepcopy(request); opts=request['questions']['q']['criteria']
            baseline['questions']['q']['criteria']={key:opts[key] for key in old['request']['questions']['q']['criteria']}
            assert baseline==old['request']
            observation=frame['result']['metrics']
            scoped=f" The tool observed {observation['entities_observed']} entities within {observation['search_radius']} blocks."
            variants={'baseline':baseline}
            for label in ('raw_observation','prose_and_observation'):
                current=copy.deepcopy(request)
                if label=='prose_and_observation':
                    current['state']['finished_call']=current['state']['finished_call'].replace(
                        'target_not_found','the selected entity was not found (target_not_found)')
                current['state']['finished_call']+=scoped
                variants[label]=current
            await ask_orders(sink,'unrelated_target',variants,'call_agent',old['choice'])
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))

asyncio.run(main())
