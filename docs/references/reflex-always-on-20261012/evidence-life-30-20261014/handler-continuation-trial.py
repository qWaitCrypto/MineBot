"""Recorded handler result; context candidates only, no runtime deployment."""
import asyncio
import copy
import itertools
import json
from collections import Counter
from pathlib import Path
import sys
from types import SimpleNamespace

root = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(root))
from minebot.app.body_loop import BodyLoop
from minebot.app.work_runtime import WorkPlanExecutor, NodeRecord
from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient


async def main():
    rows=[json.loads(line) for line in (root/'logs/reflex-always-on-20261014-interrupt-instruction-fixed/trace.jsonl').read_text().splitlines()]
    row=next(r for r in rows if r['seq']==718)
    ci=row['compiler_input']
    node={'kind':'call','tool':row['tool'],'arguments':ci['params'],'intent':ci['supervision_context']['intent']}
    plan={'language':'minebot-work/0.2','goal':ci['supervision_context']['goal'],'intent':ci['supervision_context']['intent_chain'][0],
          'timeout_s':180,'work':node}
    executor=WorkPlanExecutor(plan,call=None)
    record=NodeRecord(decision_facts=ci['decision_context'])
    baseline=executor._call_question(node, 'handler', ci['supervision_context']['intent_chain'][:-1], record)
    assert baseline==row['request']
    original=next(r for r in rows if r['event']=='call_supervision_request' and r.get('purpose')=='running_frame'
        and r.get('tool_call_id')=='plan-legacy:5b55346eff90e448:0')
    original_call=rq.call_text(original['tool'],original['compiler_input']['params'])
    client=DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    output=root/'logs/handler-continuation-trial-0a2840b.jsonl'
    report={'source_seq':718,'original_call':original_call,'candidate_only':True}
    try:
        with output.open('x') as sink:
            response=await client.ask(baseline)
            selected=rq.frame_choice(response.raw,baseline['questions'])
            report['baseline']=selected
            report['recorded']='call_agent'
            sink.write(json.dumps({'variant':'baseline','request':baseline,'response':response.raw,'choice':selected},ensure_ascii=False)+'\n')
            sink.flush()
            if selected!='call_agent':
                report['status']='baseline_not_reproduced'
                print(json.dumps(report),flush=True)
                return
            report['candidates']={}
            for variant in ('continuation_option','suspended_context','both'):
                next_node={**node,'next_call':original_call} if variant in {'continuation_option','both'} else node
                request=executor._call_question(next_node,'handler',ci['supervision_context']['intent_chain'][:-1],record)
                if variant in {'suspended_context','both'}:
                    request['state']['suspended_work']=f'This call is the interruption handler for {original_call}. The earlier call is suspended.'
                options=request['questions']['q']['criteria']
                orders=list(itertools.permutations(options))
                counts=Counter()
                for index,order in enumerate((orders*12)[:12]):
                    current=copy.deepcopy(request)
                    current['questions']['q']['criteria']={key:options[key] for key in order}
                    response=await client.ask(current)
                    selected=rq.frame_choice(response.raw,current['questions'])
                    counts[selected]+=1
                    sink.write(json.dumps({'variant':variant,'order':index,'request':current,'response':response.raw,'choice':selected},ensure_ascii=False)+'\n')
                    sink.flush()
                report['candidates'][variant]=dict(counts)
                print(json.dumps({'variant':variant,'distribution':dict(counts)},ensure_ascii=False),flush=True)
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

asyncio.run(main())
