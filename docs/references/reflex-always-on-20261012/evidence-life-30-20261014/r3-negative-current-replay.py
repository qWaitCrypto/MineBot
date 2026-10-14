"""Native-only captures via current online projector and plan result compiler."""
import asyncio
import copy
import itertools
import json
from collections import Counter
from pathlib import Path
import sys

root = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(root))
from minebot.app import reflex_question as rq
from minebot.app.runner import _model_tool_payload
from minebot.app.work_runtime import WorkPlanExecutor, NodeRecord
from minebot.app.decision_client import DecisionClient


async def main():
    output = root / 'logs/r3-negative-current-replay-0a2840b.jsonl'
    previous = [json.loads(line) for line in (root / 'logs/step6-replay-v2.jsonl').read_text().splitlines()]
    report = []
    client = DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    try:
        with output.open('x') as sink:
            for name in ('broken-tool-frame-v3', 'unrelated-frame'):
                frame = json.loads((root / 'logs/reflex-always-on-20261014c-step6-native' / (name + '.json')).read_text())
                projected = _model_tool_payload(frame['tool'], frame['result'], trace_ref=name)
                plan = {'language':'minebot-work/0.2', 'goal':frame['intent'], 'intent':frame['intent'], 'timeout_s':30,
                        'work':{'kind':'call', 'id':'fight', 'tool':frame['tool'], 'arguments':frame['arguments'], 'intent':frame['intent']}}
                executor = WorkPlanExecutor(plan, call=None)
                record = NodeRecord(decision_facts={'tool_result':projected, 'tool_arguments':frame['arguments'], 'body_state':frame['facts']})
                request = executor._call_question(plan['work'], 'fight', [frame['intent']], record)
                old = next(r for r in previous if r['name']==name)
                baseline = copy.deepcopy(request)
                options = baseline['questions']['q']['criteria']
                baseline['questions']['q']['criteria'] = {key:options[key] for key in old['request']['questions']['q']['criteria']}
                assert baseline == old['request']
                answer = await client.ask(baseline)
                chosen = rq.frame_choice(answer.raw, baseline['questions'])
                item = {'name':name, 'recorded':old['choice'], 'baseline':chosen,
                        'source':'native-only capture, not Agent evidence'}
                report.append(item)
                sink.write(json.dumps({'name':name,'variant':'baseline','request':baseline,'response':answer.raw,
                                       'choice':chosen},ensure_ascii=False)+'\n')
                sink.flush()
                if chosen != old['choice']:
                    item['status']='baseline_not_reproduced'
                    print(json.dumps(item),flush=True)
                    break
                orders = list(itertools.permutations(options))
                counts = Counter()
                for index, order in enumerate((orders*12)[:12]):
                    current = copy.deepcopy(request)
                    current['questions']['q']['criteria']={key:options[key] for key in order}
                    answer=await client.ask(current)
                    choice=rq.frame_choice(answer.raw,current['questions'])
                    counts[choice]+=1
                    sink.write(json.dumps({'name':name,'variant':index,'request':current,'response':answer.raw,
                                           'choice':choice,'elapsed_s':answer.elapsed_s},ensure_ascii=False)+'\n')
                    sink.flush()
                item['choices']=dict(counts)
                print(json.dumps(item),flush=True)
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

asyncio.run(main())
