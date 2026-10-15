"""Premature next_step on running frames: replay recorded raw frames, compare wordings.

Source: logs/reflex-always-on-20261014-step2-current-4b83331 (code 4b83331).
Every request is first rebuilt from the recorded frame_input/compiler_input by
the online compiler and must equal the recorded request byte for byte; the
recorded request keeps the compiler's key order (live order).  Then:

  1. baseline: each rebuilt request once, live order -> must match the recorded choice;
  2. variants: only the wording named below changes; 6 orders of the q options x 2.

Labels are the reviewer's reading of the Agent's intent against the frame's own
inventory / main-hand facts; they are bench labels, not product logic.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.body_loop import BodyLoop  # noqa: E402
from minebot.app.decision_client import DecisionClient  # noqa: E402
from minebot.app import reflex_question as rq  # noqa: E402

RUN = ROOT / 'logs/reflex-always-on-20261014-step2-current-4b83331'
OUT = ROOT / 'logs/step2-done-wording-trial-20261014.jsonl'
WORKERS = 4
ONLY = set(sys.argv[1].split(',')) if len(sys.argv) > 1 and sys.argv[1] else None
if ONLY:
    OUT = OUT.with_name(OUT.stem + '-probe-' + '-'.join(sorted(ONLY)) + '.jsonl')


def label(intent: str, facts: dict) -> str | None:
    """met / unmet, or None when the intent is not an inventory/main-hand fact."""
    inv = {str(k).removeprefix('minecraft:'): v for k, v in (facts.get('inventory') or {}).items()}
    held = str(facts.get('selected_item') or '').removeprefix('minecraft:')
    m = re.search(r'(\d+)\s*(?:个|把)\s*([a-z_]+|原木)', intent)
    if not m:
        if '主手' in intent:
            item = re.search(r'([a-z_]+)', intent)
            return None if not item else ('met' if held == item.group(1) else 'unmet')
        return None
    need, item = int(m.group(1)), m.group(2)
    have = sum(v for k, v in inv.items() if k.endswith('_log')) if item == '原木' else inv.get(item, 0)
    ok = have >= need
    if '主手' in intent:
        ok = ok and held == item
    return 'met' if ok else 'unmet'


def variant(request: dict, name: str, intent: str, call: str) -> dict:
    sent = copy.deepcopy(request)
    crit = sent['questions']['q']['criteria']
    finished = 'Stop the current action and report to the Agent that this work is finished.'
    shown = f'The inventory and Body facts above already show “{intent}”: stop the current action and report it to the Agent.'
    if name in ('v1_finished', 'v2_ab602a2'):
        if 'next_step' in crit:
            crit['next_step'] = finished
    if name == 'v2_ab602a2':
        sent['state']['agent_intent'] = intent
        sent['questions']['q']['instructions'] = sent['questions']['q']['instructions'].replace(
            "Choose what the Body does right now to achieve the Agent's intent.", 'Choose what the Body does next.')
    if name in ('v3_shown', 'v4_shown_both'):
        if 'next_step' in crit:
            crit['next_step'] = shown
    if name == 'v4_shown_both' and 'keep_going' in crit:
        crit['keep_going'] = (f'Keep doing the current work, {call}, because the inventory and Body facts '
                              f'above do not show “{intent}” yet.')
    if name == 'v5_question_a':
        sent['questions']['a'] = {
            'type': 'choice',
            'instructions': 'Do the inventory and Body facts above already show the result the Agent wants?',
            'criteria': {'shown': f'Yes: the facts above already show “{intent}”.',
                         'not_shown': f'No: the facts above do not show “{intent}” yet.'},
        }
    return sent


def ordered(request: dict, order: tuple[str, ...]) -> dict:
    sent = copy.deepcopy(request)
    crit = sent['questions']['q']['criteria']
    sent['questions']['q']['criteria'] = {k: crit[k] for k in order}
    return sent


def orders(keys: list[str]) -> list[tuple[str, ...]]:
    perms = list(itertools.permutations(keys))
    if len(perms) <= 6:
        return perms * 2
    return perms[::max(1, len(perms) // 12)][:12]


async def main() -> None:
    goal = json.loads((RUN / 'launcher-owner.json').read_text())['goal']
    rows = [json.loads(line) for line in (RUN / 'trace.jsonl').open()]
    frames = [r for r in rows if r['event'] == 'call_supervision_request' and r.get('purpose') == 'running_frame']
    if ONLY:
        frames = [r for r in frames if str(r['seq']) in ONLY]
    results = {r['tool_call_id'] + ':' + str(r['seq']): r for r in rows if r['event'] == 'call_supervision_result'}
    def recorded_choice(frame):
        return next((r['choice'] for r in rows if r['event'] == 'call_supervision_result'
                     and r['seq'] > frame['seq'] and r['tool_call_id'] == frame['tool_call_id']
                     and r.get('purpose') == 'running_frame'), None)

    jobs = []
    meta = {}
    probe = BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal), decision_client=None,
                                     trace=SimpleNamespace(emit=lambda *a, **k: None)))
    for frame in frames:
        ci, execution = frame['compiler_input'], frame['frame_input']['execution']
        rebuilt = probe._question(frame['tool_call_id'], SimpleNamespace(name=frame['tool']), ci['params'], execution,
                                  idle_s=frame['idle_s'], purpose='running_frame',
                                  supervision_context=ci['supervision_context'], options=ci['options'],
                                  decision_context=ci['decision_context'])
        rebuilt.pop('_request_chars', None)
        rebuilt.pop('_request_chars_before', None)
        equal = json.dumps(rebuilt, ensure_ascii=False) == json.dumps(frame['request'], ensure_ascii=False)
        intent = str(ci['supervision_context'].get('intent') or ci['supervision_context'].get('node_description') or '')
        call = rq.call_text(frame['tool'], ci['params'])
        meta[frame['seq']] = {'tool': frame['tool'], 'intent': intent, 'byte_equal': equal,
                              'label': label(intent, execution.get('facts') or {}),
                              'recorded': recorded_choice(frame)}
        if not equal:
            continue
        jobs.append((frame['seq'], 'baseline', 0, rebuilt))
        keys = list(rebuilt['questions']['q']['criteria'])
        for name in ('v0_current', 'v1_finished', 'v2_ab602a2', 'v3_shown', 'v4_shown_both', 'v5_question_a'):
            base = variant(rebuilt, name, intent, call)
            for index, order in enumerate(orders(keys)):
                jobs.append((frame['seq'], name, index, ordered(base, order)))

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
                    seq, name, index, request = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    answer = await client.ask(request)
                    choice = rq.frame_choice(answer.raw, request['questions'])
                    raw = answer.raw
                    if 'a' in request['questions']:
                        a = ((raw or {}).get('answers') or {}).get('a') or {}
                        tally[(seq, 'v5_a_only')][str(a.get('choice'))] += 1
                except Exception as exc:  # recorded as unknown, never retried silently
                    choice, raw = 'unknown', {'error': type(exc).__name__}
                tally[(seq, name)][choice] += 1
                out.write(json.dumps({'seq': seq, 'variant': name, 'order': index, 'choice': choice,
                                      'request': request, 'response': raw}, ensure_ascii=False) + '\n')
                out.flush()
        finally:
            await client.close()

    await asyncio.gather(*(worker() for _ in range(WORKERS)))
    out.close()
    summary = {'source': str(RUN), 'code': '4b83331', 'frames': []}
    for seq, info in meta.items():
        row = dict(info, seq=seq)
        for name in ('baseline', 'v0_current', 'v1_finished', 'v2_ab602a2', 'v3_shown', 'v4_shown_both', 'v5_question_a', 'v5_a_only'):
            if (seq, name) in tally:
                row[name] = dict(tally[(seq, name)])
        summary['frames'].append(row)
    OUT.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    # scoreboard
    board = defaultdict(lambda: defaultdict(Counter))
    for row in summary['frames']:
        for name in ('v0_current', 'v1_finished', 'v2_ab602a2', 'v3_shown', 'v4_shown_both', 'v5_question_a', 'v5_a_only'):
            for choice, n in row.get(name, {}).items():
                board[name][row['label']][choice] += n
    base_ok = sum(1 for r in summary['frames'] if r.get('baseline') and r['recorded'] in r['baseline'])
    print('byte_equal', sum(1 for r in summary['frames'] if r['byte_equal']), '/', len(summary['frames']),
          'baseline reproduced', base_ok)
    for name, by_label in board.items():
        print(name, {k: dict(v) for k, v in by_label.items()})


asyncio.run(main())
