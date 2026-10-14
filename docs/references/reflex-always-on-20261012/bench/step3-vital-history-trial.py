"""Raw recorded interruptions: compare factual histories before product edits."""
import asyncio
import copy
import itertools
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq
from minebot.app import direct_call_supervision as supervision


def restore_body(rows, question, attempt_index):
    """Take facts from the running question that selected the protective tool."""
    source_questions = [r for r in rows if r['event']=='call_supervision_request'
        and r.get('purpose')=='default_result_frame'
        and r['tool_call_id']==question['tool_call_id'] and r['seq']<=question['seq']]
    source = source_questions[attempt_index]
    selection = next(r for r in reversed(rows) if r['seq']<source['seq']
        and r['event']=='call_supervision_result' and r.get('choice')=='surface'
        and r['tool_call_id']==question['tool_call_id'])
    frame = next(r for r in reversed(rows) if r['seq']<selection['seq']
        and r['event']=='call_supervision_request'
        and r['tool_call_id']==selection['tool_call_id']
        and r.get('fact_revision')==selection.get('fact_revision'))
    raw = frame['frame_input']
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace()))
    loop._ambient_facts = copy.deepcopy(raw['ambient_facts'])
    loop._vitals._samples = copy.deepcopy(raw['vital_samples'])
    loop.safe_standing_position = copy.deepcopy(raw['safe_standing_position'])
    with patch.object(supervision.time, 'monotonic', return_value=raw['monotonic_s']):
        facts = loop._frame(raw['execution'])['facts']
    return facts, frame['seq']


def trial_lines(history, style):
    lines = []
    for n, attempt in enumerate(history, 1):
        facts = attempt['body']
        receipt = rq._interruption(attempt['result'])
        default = receipt['default_action']
        original = rq.result_sentence('the original collection', attempt['result'])
        body = rq._body_sentence(facts)
        inventory = facts.get('inventory', facts.get('inventory_counts'))
        inventory_text = ', '.join(f'{k} {v}' for k,v in inventory.items()) or 'empty'
        position = rq._position(facts.get('position', facts.get('pos')))
        protection = rq.result_sentence(default['tool']+'()', default.get('result'))
        if style == 'body_at_interruption':
            text = (f'Attempt {n}: {original} At interruption: {body} '
                    f'Inventory: {inventory_text}. Position: {position}. {protection}')
        elif style == 'chronological':
            text = (f'On attempt {n}, the Body was doing the same collection. {body} '
                    f'{original} It then ran {default["tool"]}(). {protection} '
                    f'Inventory at interruption: {inventory_text}. Position: {position}.')
        else:
            text = (f'Attempt {n} of the same original work: {body} '
                    f'Inventory at interruption: {inventory_text}. Position: {position}. '
                    f'{original} Protective action: {protection}')
        lines.append(text)
    return lines


async def main():
    run = ROOT/'logs/reflex-always-on-20261014-step3-current'
    rows = [json.loads(l) for l in (run/'trace.jsonl').open()]
    frames = [r for r in rows if r['event']=='call_supervision_request'
        and r.get('purpose')=='default_result_frame'][:5]
    goal = json.loads((run/'acceptance-run.json').read_text())['goal']
    path = ROOT/'logs/step3-vital-history-trial-edc89ff-v2.jsonl'
    path.touch(exist_ok=False)
    client = DecisionClient()
    report = []
    loop = BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal), decision_client=client))
    try:
        with path.open('w') as sink:
            for ordinal, row in enumerate(frames, 1):
                inp = row['compiler_input']
                ctx = inp['decision_context']
                original = next(r for r in reversed(rows) if r['seq']<row['seq']
                    and r['event']=='call_supervision_request' and r['tool_call_id']==row['tool_call_id']
                    and r.get('purpose')=='running_frame')
                options = rq.after_default_options(
                    original_call=rq.call_text(original['tool'], original['compiler_input']['params']),
                    intent=inp['supervision_context']['intent'],
                    default_call=rq.call_text(row['tool'], inp['params']),
                    result=ctx['tool_result'], facts=ctx['body_state'])
                generated = loop._question(row['tool_call_id'], SimpleNamespace(name=row['tool']),
                    inp['params'], row['frame_input']['execution'], idle_s=row['idle_s'],
                    purpose=row['purpose'], supervision_context=inp['supervision_context'],
                    options=options, decision_context=ctx)
                generated.pop('_request_chars',None)
                generated.pop('_request_chars_before',None)
                assert json.dumps(generated,ensure_ascii=False)==json.dumps(row['request'],ensure_ascii=False)
                previous = next(r for r in rows if r['event']=='call_supervision_result'
                    and r['seq']>row['seq'] and r['tool_call_id']==row['tool_call_id']
                    and r.get('purpose')==row['purpose'])['choice']
                baseline = await client.ask(generated)
                choice = rq.frame_choice(baseline.raw,generated['questions'])
                sink.write(json.dumps({'seq':row['seq'],'variant':'baseline','request':generated,
                    'response':baseline.raw,'choice':choice,'recorded':previous},ensure_ascii=False)+'\n')
                sink.flush()
                if choice!=previous:
                    report.append({'seq':row['seq'],'baseline_mismatch':True,'choice':choice,'recorded':previous})
                    break
                history = copy.deepcopy(ctx['interrupted_history'])
                sources = []
                for index, attempt in enumerate(history):
                    facts, source = restore_body(rows,row,index)
                    attempt['body'] = {**facts, **attempt['body']}
                    sources.append(source)
                variants = {s:trial_lines(history,s) for s in
                    ('body_at_interruption','chronological','body_then_result')}
                if ordinal==1:
                    variants['no_history_control'] = []
                counts = {s:Counter() for s in variants}
                for permutation, order in enumerate(list(itertools.permutations(options))*2):
                    for style, lines in variants.items():
                        sent = copy.deepcopy(generated)
                        position = sent['state'].pop('position',None)
                        sent['state'].pop('interruptions_of_the_original_work',None)
                        if lines:sent['state']['interruptions_of_the_original_work']=lines
                        if position:sent['state']['position']=position
                        sent['questions']['q']['criteria']={k:options[k] for k in order}
                        answer = await client.ask(sent)
                        selected = rq.frame_choice(answer.raw,sent['questions'])
                        counts[style][selected]+=1
                        sink.write(json.dumps({'seq':row['seq'],'attempt':ordinal,'variant':style,
                            'permutation':permutation,'source_frames':sources,'request':sent,
                            'response':answer.raw,'choice':selected,'elapsed_s':answer.elapsed_s},ensure_ascii=False)+'\n')
                        sink.flush()
                item = {'seq':row['seq'],'attempt':ordinal,'source_frames':sources,
                    'baseline_reproduced':True,'distributions':{s:dict(c) for s,c in counts.items()}}
                report.append(item)
                print(json.dumps(item,ensure_ascii=False),flush=True)
    finally:
        await client.close()
        path.with_suffix('.summary.json').write_text(json.dumps({'frames':report},ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())
