"""Undeployed failure-prose candidate, with no option/admission changes."""
import asyncio
import copy
import itertools
import json
from collections import Counter
from pathlib import Path
import sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app import reflex_question as rq
from minebot.app.runner import _model_tool_payload
from minebot.app.work_runtime import WorkPlanExecutor, NodeRecord
from minebot.app.decision_client import DecisionClient

async def main():
    root = Path('/home/qwait/MineBot-always-on')
    frame = json.loads((root / 'logs/reflex-always-on-20261014c-step6-native/unrelated-frame.json').read_text())
    projected = _model_tool_payload(frame['tool'], frame['result'], trace_ref='unrelated-frame')
    node = {'kind': 'call', 'id': 'fight', 'tool': frame['tool'], 'arguments': frame['arguments'], 'intent': frame['intent']}
    plan = {'language': 'minebot-work/0.2', 'goal': frame['intent'], 'intent': frame['intent'], 'timeout_s': 30, 'work': node}
    executor = WorkPlanExecutor(plan, call=None)
    record = NodeRecord(decision_facts={'tool_result': projected, 'tool_arguments': frame['arguments'], 'body_state': frame['facts']})
    request = executor._call_question(node, 'fight', [frame['intent']], record)
    earlier = [json.loads(line) for line in (root / 'logs/r3-negative-current-replay-0a2840b.jsonl').read_text().splitlines()]
    old = next(r for r in earlier if r['name'] == 'unrelated-frame' and r['variant'] == 'baseline')
    baseline = copy.deepcopy(request)
    options = baseline['questions']['q']['criteria']
    baseline['questions']['q']['criteria'] = {key: options[key] for key in old['request']['questions']['q']['criteria']}
    assert baseline == old['request']
    client = DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    output = root / 'logs/r3-target-reason-trial-5703f3f.jsonl'
    report = {'candidate_only': True, 'source': 'native-only capture, not Agent evidence', 'recorded': old['choice']}
    try:
        with output.open('x') as sink:
            answer = await client.ask(baseline)
            chosen = rq.frame_choice(answer.raw, baseline['questions'])
            report['baseline'] = chosen
            sink.write(json.dumps({'variant': 'baseline', 'request': baseline, 'response': answer.raw, 'choice': chosen}, ensure_ascii=False) + '\n')
            sink.flush()
            if chosen != old['choice']:
                report['status'] = 'baseline_not_reproduced; no further comparison'
            else:
                candidate = copy.deepcopy(request)
                candidate['state']['finished_call'] = candidate['state']['finished_call'].replace(
                    'target_not_found', 'the selected entity was not found (target_not_found)')
                options = candidate['questions']['q']['criteria']
                counts = Counter()
                for order in (list(itertools.permutations(options)) * 12)[:12]:
                    current = copy.deepcopy(candidate)
                    current['questions']['q']['criteria'] = {key: options[key] for key in order}
                    answer = await client.ask(current)
                    chosen = rq.frame_choice(answer.raw, current['questions'])
                    counts[chosen] += 1
                    sink.write(json.dumps({'variant': 'failure_prose', 'request': current, 'response': answer.raw, 'choice': chosen}, ensure_ascii=False) + '\n')
                    sink.flush()
                report['distribution'] = dict(counts)
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report), flush=True)

asyncio.run(main())
