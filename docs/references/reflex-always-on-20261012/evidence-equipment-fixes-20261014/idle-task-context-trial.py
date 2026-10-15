import asyncio
import copy
import itertools
import json
from collections import Counter
from pathlib import Path
import sys
from types import SimpleNamespace
ROOT=Path('/home/qwait/MineBot-always-on')
sys.path.insert(0,str(ROOT))
from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq

async def main():
    rows=[json.loads(line) for line in (ROOT/'logs/reflex-always-on-20261014-idle-equipment-fixed/trace.jsonl').read_text().splitlines()]
    answer=next(r for r in rows if r['seq']==440)
    row=next(r for r in reversed(rows) if r['seq']<440 and r['event']=='call_supervision_request' and r.get('tool_call_id')==answer['tool_call_id'] and r.get('fact_revision')==answer.get('fact_revision'))
    inp=row['compiler_input']
    loop=BodyLoop(SimpleNamespace(decision_client=SimpleNamespace(),agent_context=SimpleNamespace(goal_text=row['request']['state']['task'])))
    ctx=inp['supervision_context']
    execution=copy.deepcopy(row['frame_input']['execution'])
    # This frame has no falling vitals; the recorded raw execution contains
    # all body/progress facts used by its running question.
    record={'tool':row['tool'],'params':inp['params'],'supervision_context':ctx}
    options=loop._options_for(SimpleNamespace(name=row['tool']),record,execution=execution)
    def compile_context(context):
        request=loop._question(row['tool_call_id'],SimpleNamespace(name=row['tool']),inp['params'],execution,
            idle_s=row['idle_s'],purpose=row['purpose'],supervision_context=context,options=options,
            decision_context=inp['decision_context'])
        request.pop('_request_chars',None);request.pop('_request_chars_before',None)
        return request
    baseline=compile_context(ctx)
    assert json.dumps(baseline,ensure_ascii=False)==json.dumps(row['request'],ensure_ascii=False)
    candidate=compile_context({**ctx,'goal':''})
    expected=copy.deepcopy(baseline);expected['state'].pop('task')
    assert candidate==expected, {key:(expected['state'].get(key),candidate['state'].get(key))
        for key in set(expected['state'])|set(candidate['state']) if expected['state'].get(key)!=candidate['state'].get(key)}
    client=DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    path=ROOT/'logs/idle-task-context-trial-58145e2.jsonl'
    report={};counts=Counter()
    try:
        with path.open('x') as sink:
            result=await client.ask(baseline)
            choice=rq.frame_choice(result.raw,baseline['questions'])
            report['baseline']={'recorded':'call_agent','selected':choice,'matched':choice=='call_agent'}
            sink.write(json.dumps({'variant':'baseline','request':baseline,'response':result.raw,'choice':choice},ensure_ascii=False)+'\n');sink.flush()
            if choice!='call_agent':
                print(json.dumps(report));return
            orders=list(itertools.permutations(options))*2
            for index,order in enumerate(orders):
                request=copy.deepcopy(candidate)
                request['questions']['q']['criteria']={k:options[k] for k in order}
                answer=await client.ask(request)
                choice=rq.frame_choice(answer.raw,request['questions'])
                counts[choice]+=1
                sink.write(json.dumps({'variant':'clear_finished_task','order':index,'request':request,'response':answer.raw,'choice':choice},ensure_ascii=False)+'\n');sink.flush()
            report['distribution']=dict(counts)
            print(json.dumps(report,ensure_ascii=False))
    finally:
        await client.close()
        path.with_suffix('.summary.json').write_text(json.dumps(report,indent=2))

if __name__=='__main__':asyncio.run(main())
