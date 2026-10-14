"""Prospective resume question with the captured post-handler Body facts."""
import asyncio
import itertools
import json
from collections import Counter
from pathlib import Path
import sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient

async def main():
    root = Path('/home/qwait/MineBot-always-on')
    rows = [json.loads(line) for line in (root / 'logs/reflex-always-on-20261014-interrupt-instruction-fixed/trace.jsonl').read_text().splitlines()]
    handler = next(r for r in rows if r['seq'] == 718)
    running = next(r for r in rows if r['event'] == 'call_supervision_request'
        and r.get('purpose') == 'running_frame' and r.get('tool_call_id') == 'plan-legacy:5b55346eff90e448:0')
    ci = handler['compiler_input']
    original = running['compiler_input']
    request = rq.interrupt_result_question(goal=original['supervision_context']['goal'],
        intent=original['supervision_context']['intent'],
        original_call=rq.call_text(running['tool'], original['params']),
        handler_intent=ci['supervision_context']['intent'],
        handler_call=rq.call_text(handler['tool'], ci['params']),
        facts=ci['decision_context']['body_state'])
    options = request['questions']['q']['criteria']
    client = DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    counts = Counter()
    output = root / 'logs/handler-resume-frame-trial-0a2840b.jsonl'
    try:
        with output.open('x') as sink:
            orders = list(itertools.permutations(options))
            for index, order in enumerate((orders * 12)[:12]):
                current = {**request, 'questions': rq.frame_questions({key: options[key] for key in order})}
                answer = await client.ask(current)
                choice = rq.frame_choice(answer.raw, current['questions'])
                counts[choice] += 1
                sink.write(json.dumps({'order': index, 'request': current, 'response': answer.raw,
                                      'choice': choice}, ensure_ascii=False) + '\n')
                sink.flush()
    finally:
        await client.close()
    summary = {'source_body_seq': 718, 'prospective_frame_not_observed_online': True,
               'distribution': dict(counts), 'expected': 'resume'}
    output.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary), flush=True)

asyncio.run(main())
