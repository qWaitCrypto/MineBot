"""New terminal path, using the real idle broken-weapon receipt and next raw read."""
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
from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient

async def main():
    run=ROOT/'logs/reflex-always-on-20261014-default-equipment-fixed'
    rows=[json.loads(line) for line in (run/'trace.jsonl').read_text().splitlines()]
    terminal=next(r for r in rows if r['event']=='tool_raw_result' and r.get('tool')=='engage_entity' and (r.get('raw_result') or {}).get('reason')=='tool_broke')
    frame=next(r for r in rows if r['seq']>terminal['seq'] and r['event']=='call_supervision_request' and r.get('purpose')=='idle_frame')
    facts=frame['compiler_input']['facts']
    receipt=terminal['raw_result']
    params=terminal['arguments']
    # This is a new result question, not a claim that a historical terminal
    # request existed. Facts are from the recorded read immediately following
    # the real terminal; no edited body facts or invented target are used.
    loop=BodyLoop(SimpleNamespace(decision_client=SimpleNamespace()))
    options=rq.after_default_options(original_call='',intent=rq.DEFAULT_ACTION_INTENTS['engage'],
        default_call=rq.call_text('engage_entity',params),result=receipt,facts=facts)
    counts=Counter(); report={'terminal_seq':terminal['seq'],'body_read_seq':frame['seq']}
    client=DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    path=ROOT/'logs/idle-equipment-trial-6053ca4.jsonl'
    try:
        with path.open('x') as sink:
            baseline=rq.idle_question(facts,rq.idle_options(facts))
            assert json.dumps(baseline,ensure_ascii=False)==json.dumps(frame['request'],ensure_ascii=False)
            answer=await client.ask(baseline)
            choice=rq.frame_choice(answer.raw,baseline['questions'])
            previous=next(r['choice'] for r in rows if r['seq']>frame['seq'] and r['event']=='call_supervision_result' and r.get('purpose')=='idle_frame')
            report['idle_baseline']={'selected':choice,'recorded':previous,'matched':choice==previous}
            sink.write(json.dumps({'variant':'idle_baseline','request':baseline,'response':answer.raw,'choice':choice},ensure_ascii=False)+'\n');sink.flush()
            if choice!=previous:
                print(json.dumps(report));return
            orders=list(itertools.permutations(options))*2
            for n,order in enumerate(orders):
                request=loop._question('idle-terminal',SimpleNamespace(name='engage_entity'),params,
                    {'facts':facts,'result':receipt,'state':'failed'},idle_s=0,purpose='default_result_frame',
                    supervision_context={'goal':'','intent':rq.DEFAULT_ACTION_INTENTS['engage'],'is_reflex_action':True,'idle_protection':True},
                    options={key:options[key] for key in order},decision_context={'tool_result':receipt,'body_state':facts})
                request.pop('_request_chars',None);request.pop('_request_chars_before',None)
                answer=await client.ask(request)
                choice=rq.frame_choice(answer.raw,request['questions'])
                counts[choice]+=1
                sink.write(json.dumps({'variant':'idle_result','order':n,'request':request,'response':answer.raw,'choice':choice,'elapsed_s':answer.elapsed_s},ensure_ascii=False)+'\n');sink.flush()
            report['result_distribution']=dict(counts)
            print(json.dumps(report,ensure_ascii=False))
    finally:
        await client.close()
        path.with_suffix('.summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
