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
from unittest.mock import patch

root = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(root))
definitions = (root / 'logs/interrupt-condition-trial.py').read_text().split('async def main():')[0]
definitions = definitions.replace("assert request == row['request'], f\"Compilation differs for seq {row['seq']}\"", '')
exec(compile(definitions, __file__, 'exec'))
from minebot.app import direct_call_supervision as supervision


async def main():
    rows = [json.loads(line) for line in (root / 'logs/reflex-always-on-20261014-step6-next/trace.jsonl').read_text().splitlines()]
    output = root / 'logs/interrupt-online-trial-v2.jsonl'
    report = []
    client = DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    try:
        with tempfile.TemporaryDirectory(prefix='minebot-interrupt-legacy-') as folder:
            path = Path(folder) / 'reflex_question.py'
            path.write_bytes(subprocess.check_output(['git', 'show', '648e073:minebot/app/reflex_question.py'], cwd=root))
            spec = importlib.util.spec_from_file_location('legacy_interrupt_question', path)
            old = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old)
            old_questions = old.frame_questions
            old.frame_questions = lambda options, interrupt=None, **kwargs: old_questions(options, **kwargs)
            with output.open('x') as sink:
                for seq in (298, 475, 563):
                    row = next(r for r in rows if r['seq'] == seq)
                    with patch.object(supervision, 'rq', old):
                        baseline = compile_online(row)
                    assert baseline == row['request']
                    answer = await client.ask(baseline)
                    previous = next(r['choice'] for r in rows if r['seq'] > seq and r['event'] == 'call_supervision_result'
                                    and r.get('tool_call_id') == row['tool_call_id'] and r.get('purpose') == row['purpose'])
                    selected = rq.frame_choice(answer.raw, baseline['questions'])
                    sink.write(json.dumps({'seq': seq, 'variant': 'baseline', 'request': baseline,
                        'response': answer.raw, 'choice': selected}, ensure_ascii=False) + '\n')
                    sink.flush()
                    item = {'seq': seq, 'recorded': previous, 'baseline': selected}
                    report.append(item)
                    if selected != previous:
                        item['status'] = 'baseline_not_reproduced'
                        print(json.dumps(item), flush=True)
                        break
                    candidate = compile_online(row)
                    expected = copy.deepcopy(baseline)
                    interrupt = row['compiler_input']['supervision_context']['interrupt_when']
                    expected['questions']['q']['instructions'] += (
                        f" The Agent also says: when “{interrupt['condition']['text']}”, interrupt the current work and do “{interrupt['handler']['intent']}”.")
                    assert candidate == expected
                    counts = Counter()
                    for order in list(itertools.permutations(candidate['questions']['q']['criteria']))[:12]:
                        request = copy.deepcopy(candidate)
                        opts = request['questions']['q']['criteria']
                        request['questions']['q']['criteria'] = {key: opts[key] for key in order}
                        answer = await client.ask(request)
                        choice = rq.frame_choice(answer.raw, request['questions'])
                        counts[choice] += 1
                        sink.write(json.dumps({'seq': seq, 'variant': 'online', 'order': order,
                            'request': request, 'response': answer.raw, 'choice': choice}, ensure_ascii=False) + '\n')
                        sink.flush()
                    item['expected'] = 'keep_going' if seq == 298 else 'interrupt'
                    item['distribution'] = dict(counts)
                    print(json.dumps(item, ensure_ascii=False), flush=True)
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

asyncio.run(main())
