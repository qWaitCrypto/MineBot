"""Regression for the separate 'is the intent shown?' question (running frames)
and the reverted result-frame report wording / main hand beside the inventory.

Running frames: rebuilt by the production compiler from recorded inputs; the
recorded options lose next_step exactly as frame_options now does.
Result frames: the recorded request (code 4b83331) is the old side; the new
side applies the two named changes.  Both sides are asked in 12 orders.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.body_loop import BodyLoop  # noqa: E402
from minebot.app.decision_client import DecisionClient  # noqa: E402
from minebot.app import reflex_question as rq  # noqa: E402
from tests.unit.reflex_expected import main_hand_beside_inventory  # noqa: E402

LOGS = ROOT / 'logs'
OUT = LOGS / 'step2-done-regression-20261014.jsonl'
WORKERS = 4
RUNS = {
    'player-handler-fixed': 'reflex-always-on-20261014-player-handler-fixed',
    'life-30-final-v2': 'reflex-always-on-20261014-life-30-final-v2',
    'step2-current': 'reflex-always-on-20261014-step2-current-4b83331',
}
# Labelled running frames: (run, seq) -> the answer a real player would give.
LABELS = {('player-handler-fixed', 333): 'keep_going', ('player-handler-fixed', 862): 'keep_going'}
RESULT_PURPOSES = {'result_frame', 'plan_result_frame', 'default_result_frame'}
ACHIEVED = '” has been achieved.'


def goal_of(run: Path) -> str:
    for name in ('launcher-owner.json', 'acceptance-run.json'):
        path = run / name
        if path.exists():
            data = json.loads(path.read_text())
            if data.get('goal'):
                return data['goal']
    return ''


def new_result(request: dict, facts: dict) -> dict:
    """Old result request -> new: main hand beside the inventory, report wording reverted."""
    sent = copy.deepcopy(request)
    body = sent['state'].get('body', '')
    marker = ' The main hand holds '
    if marker in body and body.endswith('.'):
        held = body[body.index(marker) + len(marker):-1]
        sent['state']['body'] = body[:body.index(marker)]
        sent = main_hand_beside_inventory(sent, {'selected_item': '' if held == 'nothing' else held})
    if 'request_chars' in sent['state']:
        sent['state'].pop('request_chars')
        sent['state']['request_chars'] = len(json.dumps(sent, ensure_ascii=False, separators=(',', ':'), default=str))
    for question in sent['questions'].values():
        text = question.get('criteria', {}).get('next_step', '')
        if text.endswith(ACHIEVED):
            head = text[:text.index(' and report to the Agent that “')]
            question['criteria']['next_step'] = head + ' and report to the Agent that this work is finished.'
    return sent


def orders(request: dict) -> list[dict]:
    keys = list(request['questions']['q']['criteria'])
    perms = list(itertools.permutations(keys))
    perms = perms * 2 if len(perms) <= 6 else perms[::max(1, len(perms) // 12)][:12]
    out = []
    for order in perms[:12]:
        sent = copy.deepcopy(request)
        sent['questions']['q']['criteria'] = {k: request['questions']['q']['criteria'][k] for k in order}
        out.append(sent)
    return out


def choice_of(raw: dict, questions: dict) -> str:
    return rq.frame_choice(raw, questions)


async def main() -> None:
    jobs, meta = [], {}
    for tag, name in RUNS.items():
        run = LOGS / name
        goal = goal_of(run)
        probe = BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal), decision_client=None,
                                         trace=SimpleNamespace(emit=lambda *a, **k: None)))
        rows = [json.loads(line) for line in (run / 'trace.jsonl').open()
                if '"call_supervision_request"' in line]
        for row in rows:
            purpose = row.get('purpose')
            key = (tag, row['seq'])
            if purpose == 'running_frame' and tag != 'step2-current' and row.get('compiler_input'):
                ci = row['compiler_input']
                context = ci['supervision_context']
                intent = str(context.get('intent') or context.get('node_description') or '')
                if not intent or context.get('is_reflex_action') or context.get('is_life_action'):
                    continue
                options = {k: v for k, v in ci['options'].items() if k != 'next_step'}
                new = probe._question(row['tool_call_id'], SimpleNamespace(name=row['tool']), ci['params'],
                                      row['frame_input']['execution'], idle_s=row['idle_s'],
                                      purpose='running_frame', supervision_context=context,
                                      options=options, decision_context=ci['decision_context'])
                new.pop('_request_chars', None)
                new.pop('_request_chars_before', None)
                meta[key] = {'kind': 'running', 'label': LABELS.get(key), 'intent': intent}
                for side, request in (('old', row['request']), ('new', new)):
                    for index, sent in enumerate(orders(request)):
                        jobs.append((key, side, index, sent))
            elif purpose in RESULT_PURPOSES:
                facts = (row.get('compiler_input') or {}).get('decision_context', {}).get('body_state') \
                    or (row.get('frame_input') or {}).get('execution', {}).get('facts') or {}
                new = new_result(row['request'], facts)
                if json.dumps(new, ensure_ascii=False) == json.dumps(row['request'], ensure_ascii=False):
                    continue
                meta[key] = {'kind': purpose, 'label': None}
                for side, request in (('old', row['request']), ('new', new)):
                    for index, sent in enumerate(orders(request)):
                        jobs.append((key, side, index, sent))
    queue: asyncio.Queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    tally: dict = defaultdict(Counter)
    out = OUT.open('x')

    async def worker():
        client = DecisionClient()
        try:
            while True:
                try:
                    key, side, index, request = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    answer = await client.ask(request)
                    raw = answer.raw
                    choice = choice_of(raw, request['questions'])
                except Exception as exc:
                    raw, choice = {'error': type(exc).__name__}, 'unknown'
                tally[(key, side)][choice] += 1
                out.write(json.dumps({'run': key[0], 'seq': key[1], 'side': side, 'order': index,
                                      'choice': choice, 'request': request, 'response': raw},
                                     ensure_ascii=False) + '\n')
                out.flush()
        finally:
            await client.close()

    await asyncio.gather(*(worker() for _ in range(WORKERS)))
    out.close()
    frames = []
    for key, info in meta.items():
        old, new = dict(tally[(key, 'old')]), dict(tally[(key, 'new')])
        frames.append({'run': key[0], 'seq': key[1], **info, 'old': old, 'new': new,
                       'majority_changed': max(old, key=old.get) != max(new, key=new.get) if old and new else None})
    OUT.with_suffix('.summary.json').write_text(json.dumps({'frames': frames}, ensure_ascii=False, indent=1))
    for frame in frames:
        if frame['label'] or frame['majority_changed'] or frame['kind'] != 'running':
            print(json.dumps(frame, ensure_ascii=False))
    print('frames', len(frames), 'running', sum(f['kind'] == 'running' for f in frames),
          'majority changed', sum(bool(f['majority_changed']) for f in frames))


asyncio.run(main())
