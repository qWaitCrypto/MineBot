"""Recorded raw frames, online compiler, and undeployed offline candidates."""
import asyncio
import copy
import itertools
import json
from collections import Counter
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

root = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(root))
from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq


def compile_online(row):
    raw, ci = row['frame_input'], row['compiler_input']
    owner = SimpleNamespace(decision_client=SimpleNamespace(), agent_context=SimpleNamespace(goal_text=''),
                            trace=SimpleNamespace(emit=lambda *a, **k: None))
    loop = BodyLoop(owner)
    loop._vitals._samples = copy.deepcopy(raw['vital_samples'])
    loop._ambient_facts = copy.deepcopy(raw.get('ambient_facts', {}))
    loop.safe_standing_position = copy.deepcopy(raw.get('safe_standing_position'))
    with patch('time.monotonic', return_value=raw['monotonic_s']):
        frame = loop._frame(raw['execution'])
    tool = SimpleNamespace(name=row['tool'])
    record = {'tool': row['tool'], 'params': ci['params'], 'supervision_context': ci['supervision_context']}
    options = loop._options_for(tool, record, execution=frame)
    request = loop._question(row['tool_call_id'], tool, ci['params'], frame, idle_s=row['idle_s'],
                            purpose=row['purpose'], supervision_context=ci['supervision_context'],
                            options=options, decision_context=ci['decision_context'])
    request.pop('_request_chars', None)
    request.pop('_request_chars_before', None)
    assert request == row['request'], f"Compilation differs for seq {row['seq']}"
    return request


async def main():
    rows = [json.loads(line) for line in (root / 'logs/reflex-always-on-20261014-step6-next/trace.jsonl').read_text().splitlines()]
    output = root / 'logs/interrupt-condition-trial-648e073.jsonl'
    report = []
    client = DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    try:
        with output.open('x') as sink:
            for seq in (298, 475, 563):
                row = next(r for r in rows if r['seq'] == seq)
                request = compile_online(row)
                previous = next(r['choice'] for r in rows if r['seq'] > seq and r['event'] == 'call_supervision_result'
                                and r.get('tool_call_id') == row['tool_call_id'] and r.get('purpose') == row['purpose'])
                baseline = await client.ask(request)
                selected = rq.frame_choice(baseline.raw, request['questions'])
                sink.write(json.dumps({'seq': seq, 'variant': 'baseline', 'request': request,
                                       'response': baseline.raw, 'choice': selected}, ensure_ascii=False) + '\n')
                sink.flush()
                item = {'seq': seq, 'recorded': previous, 'baseline': selected}
                report.append(item)
                if selected != previous:
                    item['status'] = 'baseline_not_reproduced'
                    print(json.dumps(item), flush=True)
                    break
                interrupt = row['compiler_input']['supervision_context']['interrupt_when']
                condition, handler = interrupt['condition']['text'], interrupt['handler']['intent']
                item['expected'] = 'keep_going' if seq == 298 else 'interrupt'
                orders = list(itertools.permutations(request['questions']['q']['criteria']))[:12]
                item['candidates'] = {}
                for variant in ('instruction', 'condition_question'):
                    counts = Counter()
                    for order in orders:
                        candidate = copy.deepcopy(request)
                        opts = candidate['questions']['q']['criteria']
                        candidate['questions']['q']['criteria'] = {key: opts[key] for key in order}
                        if variant == 'instruction':
                            candidate['questions']['q']['instructions'] += (
                                f' The Agent also says: when “{condition}”, interrupt the current work and do “{handler}”.')
                        else:
                            candidate['questions']['i'] = {
                                'type': 'choice',
                                'instructions': f'Is the Agent’s interruption condition “{condition}” true right now?',
                                'criteria': {'not_yet': f'The condition “{condition}” is not true right now.',
                                             'interrupt': f'The condition “{condition}” is true right now; do “{handler}”.'},
                            }
                        answer = await client.ask(candidate)
                        choice = rq.frame_choice(answer.raw, candidate['questions'])
                        if variant == 'condition_question':
                            aux = (answer.raw.get('answers') or {}).get('i', {}).get('choice')
                            if aux == 'interrupt':
                                choice = 'interrupt'
                            elif aux != 'not_yet':
                                choice = 'unknown'
                        counts[choice] += 1
                        sink.write(json.dumps({'seq': seq, 'variant': variant, 'order': order,
                            'request': candidate, 'response': answer.raw, 'choice': choice}, ensure_ascii=False) + '\n')
                        sink.flush()
                    item['candidates'][variant] = dict(counts)
                print(json.dumps(item, ensure_ascii=False), flush=True)
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

asyncio.run(main())
