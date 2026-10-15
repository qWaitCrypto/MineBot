"""Keep recorded facts, compare old/new online compilers on corpse frames."""
import asyncio
import copy
import importlib.util
import itertools
import json
from collections import Counter
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path('/home/qwait/MineBot-always-on');sys.path.insert(0,str(ROOT))
from minebot.app.body_loop import BodyLoop
from minebot.app import direct_call_supervision as supervision
from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient

async def main():
    rows=[json.loads(line) for line in (ROOT/'logs/reflex-always-on-20261014-idle-equipment-context-fixed/trace.jsonl').read_text().splitlines()]
    client=DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    path=ROOT/'logs/dead-hostile-trial-06bca71.jsonl';report=[]
    with tempfile.TemporaryDirectory(prefix='minebot-old-hostiles-') as folder:
        old=Path(folder)/'reflex_question.py'
        old.write_bytes(subprocess.check_output(['git','show','06bca71:minebot/app/reflex_question.py'],cwd=ROOT))
        spec=importlib.util.spec_from_file_location('old_reflex_question',old)
        legacy=importlib.util.module_from_spec(spec);sys.modules[spec.name]=legacy;spec.loader.exec_module(legacy)
        try:
            with path.open('x') as sink:
                for seq in (457,464):
                    row=next(r for r in rows if r['seq']==seq);inp=row['compiler_input'];ctx=inp['decision_context']
                    loop=BodyLoop(SimpleNamespace(decision_client=SimpleNamespace()))
                    options=rq.after_default_options(original_call='',intent=inp['supervision_context']['intent'],
                        default_call=rq.call_text(row['tool'],inp['params']),result=ctx['tool_result'],facts=ctx['body_state'])
                    def compile_options(current):
                        question=loop._question(row['tool_call_id'],SimpleNamespace(name=row['tool']),inp['params'],row['frame_input']['execution'],
                            idle_s=row['idle_s'],purpose=row['purpose'],supervision_context=inp['supervision_context'],
                            options=current,decision_context=ctx)
                        question.pop('_request_chars',None);question.pop('_request_chars_before',None)
                        return question
                    with patch.object(supervision,'rq',legacy):baseline=compile_options(options)
                    assert json.dumps(baseline,ensure_ascii=False)==json.dumps(row['request'],ensure_ascii=False)
                    answer=await client.ask(baseline);choice=rq.frame_choice(answer.raw,baseline['questions'])
                    previous=next(r['choice'] for r in rows if r['seq']>seq and r['event']=='call_supervision_result' and r.get('tool_call_id')==row['tool_call_id'] and r.get('purpose')==row['purpose'])
                    item={'seq':seq,'baseline':{'selected':choice,'recorded':previous,'matched':choice==previous}}
                    sink.write(json.dumps({'seq':seq,'variant':'baseline','request':baseline,'response':answer.raw,'choice':choice},ensure_ascii=False)+'\n');sink.flush()
                    if choice!=previous:
                        report.append(item);print(json.dumps(item));break
                    candidate=compile_options(options)
                    expected=copy.deepcopy(baseline);expected['state']['threats']='No hostile mob is in view.'
                    assert candidate==expected
                    counts=Counter()
                    for index,order in enumerate(list(itertools.permutations(options))*2):
                        request=compile_options({key:options[key] for key in order})
                        result=await client.ask(request);selected=rq.frame_choice(result.raw,request['questions']);counts[selected]+=1
                        sink.write(json.dumps({'seq':seq,'variant':'exclude_known_dead','order':index,'request':request,'response':result.raw,'choice':selected},ensure_ascii=False)+'\n');sink.flush()
                    item['distribution']=dict(counts);report.append(item);print(json.dumps(item),flush=True)
        finally:
            await client.close()
            path.with_suffix('.summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
