"""Recorded-question experiment; no claim of unavailable original raw replay."""
import asyncio
import copy
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient


async def main():
    rows = [json.loads(line) for line in Path('/home/qwait/MineBot-reflex/logs/reflex-stage-20261012i/trace.jsonl').open()]
    questions = [r for r in rows if r['event'] == 'call_supervision_request' and r.get('purpose') == 'default_result_frame']
    finished = {r['tool_call_id']: r for r in rows if r['event'] == 'call_supervision_default_finished'}
    path = ROOT / 'logs/step3-history-current-6db65ec.jsonl'
    assert not path.exists()
    client = DecisionClient()
    summaries, history, prior_states = [], [], []
    try:
        with path.open('w') as output:
            for ordinal, row in enumerate(questions, 1):
                saved = row['request']
                state = saved['state']
                request = {'state': {k: state[k] for k in ('task', 'agent_intent', 'inventory', 'body', 'threats', 'finished_call', 'position') if k in state},
                           'questions': rq.frame_questions({k: saved['questions']['q']['criteria'][k] for k in ('resume', 'repeat', 'call_agent')})}
                answer = await client.ask(request)
                choice = rq.frame_choice(answer.raw, request['questions'])
                output.write(json.dumps({'seq': row['seq'], 'variant': 'original', 'choice': choice, 'request': request, 'response': answer.raw}, ensure_ascii=False) + '\n')
                output.flush()
                if choice != 'resume':
                    summaries.append({'seq': row['seq'], 'stop': 'original answer differs', 'choice': choice})
                    break
                receipt = copy.deepcopy(finished[row['tool_call_id']]['facts'])
                history.append({'result': {'success': False, 'reason': receipt['reason'], 'metrics': {'direct_call_handoff': {'facts': receipt}}}, 'body': receipt.get('body', {})})
                prior_states.append(copy.deepcopy(state))
                variants = {style: rq.interruption_lines(history, style=style) for style in ('attempts', 'events', 'outcomes')}
                # Additional factual wording trial: link the ordered episodes.
                # Preserve recorded body/inventory sentences verbatim; no
                # invented oxygen/contact samples or retry thresholds.
                chronological = []
                for index, (attempt, old) in enumerate(zip(history, prior_states), 1):
                    chronological.append(f'On attempt {index}, the Body stopped the original collection and went up to the water surface. '
                                         + f'The surface call ended normally. Inventory then: {old.get("inventory", "not recorded")}. '
                                         + (f'It then returned to the same collection. ' if index < ordinal else '')
                                         + old.get('position', ''))
                variants['chronological'] = chronological
                if ordinal == 1:
                    variants['no_history_control'] = []
                counts = {key: Counter() for key in variants}
                orders = list(itertools.permutations(request['questions']['q']['criteria'])) * 2
                for index, order in enumerate(orders):
                    for style, lines in variants.items():
                        sent = copy.deepcopy(request)
                        position = sent['state'].pop('position', None)
                        if lines:
                            sent['state']['interruptions_of_the_original_work'] = lines
                        if position:
                            sent['state']['position'] = position
                        sent['questions']['q']['criteria'] = {k: request['questions']['q']['criteria'][k] for k in order}
                        answer = await client.ask(sent)
                        choice = rq.frame_choice(answer.raw, sent['questions'])
                        counts[style][choice] += 1
                        output.write(json.dumps({'seq': row['seq'], 'attempt': ordinal, 'variant': style, 'permutation': index,
                                                 'choice': choice, 'request': sent, 'response': answer.raw, 'elapsed_s': answer.elapsed_s}, ensure_ascii=False) + '\n')
                        output.flush()
                summary = {'seq': row['seq'], 'attempt': ordinal, 'original_reproduced': True,
                           'distributions': {k: dict(v) for k, v in counts.items()}}
                summaries.append(summary)
                print(json.dumps(summary), flush=True)
    finally:
        await client.close()
    report = {'source': 'reflex-stage-20261012i', 'scope': 'Recorded question comparison, not original raw replay. Six unique option orders each repeated twice.',
              'frames': summaries}
    path.with_suffix('.summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))


asyncio.run(main())
