"""Current raw terminal frames through the exact online compiler."""
import asyncio
import copy
import itertools
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq


async def main():
    run = ROOT / 'logs/reflex-always-on-20261014-step3-current'
    goal = json.loads((run / 'acceptance-run.json').read_text())['goal']
    rows = [json.loads(line) for line in (run / 'trace.jsonl').open()]
    questions = [r for r in rows if r['event'] == 'call_supervision_request' and r.get('purpose') == 'default_result_frame'][:5]
    answers = [r for r in rows if r['event'] == 'call_supervision_result']
    client = DecisionClient()
    output = ROOT / 'logs/step3-raw-current-replay-6db65ec-v2.jsonl'
    results = []
    try:
        with output.open('x') as sink:
            for row in questions:
                inputs = row['compiler_input']
                execution = row['frame_input']['execution']
                context = inputs['decision_context']
                original = next(r for r in reversed(rows) if r['seq'] < row['seq'] and r['event'] == 'call_supervision_request'
                                and r['tool_call_id'] == row['tool_call_id'] and r.get('purpose') == 'running_frame')
                original_params = original['compiler_input']['params']
                original_tool = original['tool']
                intent = inputs['supervision_context']['intent']
                options = rq.after_default_options(original_call=rq.call_text(original_tool, original_params), intent=intent,
                            default_call=rq.call_text(row['tool'], inputs['params']), result=context['tool_result'], facts=context['body_state'])
                loop = BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal), decision_client=client))
                tool = SimpleNamespace(name=row['tool'])
                generated = loop._question(row['tool_call_id'], tool, inputs['params'], execution, idle_s=row['idle_s'],
                            purpose=row['purpose'], supervision_context=inputs['supervision_context'], options=options, decision_context=context)
                generated.pop('_request_chars', None)
                generated.pop('_request_chars_before', None)
                assert json.dumps(generated, ensure_ascii=False) == json.dumps(row['request'], ensure_ascii=False), row['seq']
                answer = await client.ask(generated)
                choice = rq.frame_choice(answer.raw, generated['questions'])
                recorded = next(r for r in answers if r['seq'] > row['seq'] and r['tool_call_id'] == row['tool_call_id'] and r.get('purpose') == row['purpose'])['choice']
                sink.write(json.dumps({'seq': row['seq'], 'variant': 'online_order', 'choice': choice, 'recorded': recorded,
                                       'request': generated, 'response': answer.raw}, ensure_ascii=False) + '\n')
                sink.flush()
                if choice != recorded:
                    results.append({'seq': row['seq'], 'stop': 'original answer differs', 'choice': choice, 'recorded': recorded})
                    break
                from collections import Counter
                counts = {style: Counter() for style in ('attempts', 'events', 'outcomes')}
                history = context['interrupted_history']
                ordinal = int(context.get('interrupted_history_earlier', 0)) + len(history)
                for order in list(itertools.permutations(options)) * 2:
                    for style in counts:
                        changed = copy.deepcopy(context)
                        # Only the trial history wording changes. All other
                        # facts, state order and options use production code.
                        sent = loop._question(row['tool_call_id'], tool, inputs['params'], execution, idle_s=row['idle_s'],
                                    purpose=row['purpose'], supervision_context=inputs['supervision_context'],
                                    options={k: options[k] for k in order}, decision_context=changed)
                        sent.pop('_request_chars', None)
                        sent.pop('_request_chars_before', None)
                        sent['state']['interruptions_of_the_original_work'] = rq.interruption_lines(history, context.get('interrupted_history_earlier', 0), style=style)
                        answer = await client.ask(sent)
                        choice = rq.frame_choice(answer.raw, sent['questions'])
                        counts[style][choice] += 1
                        sink.write(json.dumps({'seq': row['seq'], 'attempt': ordinal, 'variant': style, 'choice': choice,
                                               'request': sent, 'response': answer.raw, 'elapsed_s': answer.elapsed_s}, ensure_ascii=False) + '\n')
                        sink.flush()
                result = {'seq': row['seq'], 'attempt': ordinal, 'byte_equal': True, 'baseline_reproduced': True,
                          'choices': {k: dict(v) for k, v in counts.items()}}
                print(json.dumps(result, ensure_ascii=False), flush=True)
                results.append(result)
    finally:
        await client.close()
    output.with_suffix('.summary.json').write_text(json.dumps({'frames': results}, ensure_ascii=False, indent=2))


asyncio.run(main())
