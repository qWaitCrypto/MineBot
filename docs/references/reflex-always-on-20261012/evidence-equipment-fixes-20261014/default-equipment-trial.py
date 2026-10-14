"""Real broken-weapon result: online compiler, recorded facts, real Decision."""
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
    rows=[json.loads(line) for line in (ROOT/'logs/reflex-always-on-20261014-step6-backup-next/trace.jsonl').read_text().splitlines()]
    row=next(r for r in rows if r['seq']==759)
    inp=row['compiler_input']; ctx=inp['decision_context']
    original=next(r for r in rows if r['event']=='call_supervision_request' and r['tool_call_id']==row['tool_call_id'] and r.get('purpose')=='running_frame')
    kwargs=dict(original_call=rq.call_text(original['tool'],original['compiler_input']['params']),
        intent=inp['supervision_context']['intent'],default_call=rq.call_text(row['tool'],inp['params']),
        result=ctx['tool_result'],facts=ctx['body_state'])
    online_options=rq.after_default_options(**kwargs)
    # The recorded compiler predated protective-result equipment options.
    # Restore exactly that baseline before testing the new online compiler.
    options={k:v for k,v in online_options.items() if not k.startswith('equip:')}
    equipment={k:v for k,v in online_options.items() if k.startswith('equip:')}
    goal=row['request']['state']['task']
    loop=BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal),decision_client=SimpleNamespace()))
    def compile_options(current):
        question=loop._question(row['tool_call_id'],SimpleNamespace(name=row['tool']),inp['params'],
            row['frame_input']['execution'],idle_s=row['idle_s'],purpose=row['purpose'],
            supervision_context=inp['supervision_context'],options=current,decision_context=ctx)
        question.pop('_request_chars',None);question.pop('_request_chars_before',None)
        return question
    baseline=compile_options(options)
    assert json.dumps(baseline,ensure_ascii=False)==json.dumps(row['request'],ensure_ascii=False)
    client=DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    path=ROOT/'logs/default-equipment-online-17af08d.jsonl'
    report={}
    try:
        with path.open('x') as sink:
            answer=await client.ask(baseline)
            selected=rq.frame_choice(answer.raw,baseline['questions'])
            previous=next(r['choice'] for r in rows if r['seq']>row['seq'] and r['event']=='call_supervision_result' and r.get('purpose')==row['purpose'] and r['tool_call_id']==row['tool_call_id'])
            report['baseline']={'selected':selected,'recorded':previous,'matched':selected==previous}
            sink.write(json.dumps({'variant':'baseline','request':baseline,'response':answer.raw,'choice':selected},ensure_ascii=False)+'\n');sink.flush()
            if selected!=previous:
                print(json.dumps(report,ensure_ascii=False));return
            options=online_options
            assert equipment=={'equip:stone_sword':'Equip stone_sword from the inventory and run the same call again: engage_entity(target="4f67989e-b18f-448d-a2a1-8c8d798ade7d").'}
            counts=Counter()
            orders=list(itertools.permutations(options))*2
            for index,order in enumerate(orders):
                request=compile_options({k:options[k] for k in order})
                result=await client.ask(request)
                choice=rq.frame_choice(result.raw,request['questions'])
                counts[choice]+=1
                sink.write(json.dumps({'variant':'equipment','seq':759,'permutation':index,'request':request,'response':result.raw,'choice':choice,'elapsed_s':result.elapsed_s},ensure_ascii=False)+'\n');sink.flush()
            report['equipment_distribution']=dict(counts)
            report['requests']=len(orders)
            print(json.dumps(report,ensure_ascii=False))
    finally:
        await client.close()
        path.with_suffix('.summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
