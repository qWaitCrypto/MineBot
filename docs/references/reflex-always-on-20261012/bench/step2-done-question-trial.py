"""Premature next_step: the proposed separate 'is the result shown?' question, four fact/question wordings.

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
OUT = ROOT / 'logs/step2-done-question-trial-20261014.jsonl'
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



MAIN_RE = re.compile(r' ?The main hand holds ([^.]*)\.')


def variant(request: dict, name: str, intent: str) -> dict:
    """q loses next_step; question a asks whether the facts show the intent."""
    sent = copy.deepcopy(request)
    crit = sent['questions']['q']['criteria']
    crit.pop('next_step', None)
    ask = 'Do the inventory and Body facts above already show the result the Agent wants?'
    if name in ('a2_every_part', 'a4_both'):
        ask = ('Compare each part of the result the Agent wants with the inventory and Body facts above. '
               'Do the facts already show every part of it?')
    if name in ('a3_main_hand', 'a4_both'):
        body = sent['state'].get('body', '')
        m = MAIN_RE.search(body)
        if m:
            state = {}
            for key, value in sent['state'].items():
                state[key] = MAIN_RE.sub('', value) if key == 'body' else value
                if key == 'inventory':
                    state['main_hand'] = m.group(1)
            if 'main_hand' not in state:
                state['main_hand'] = m.group(1)
            sent['state'] = state
    sent['questions']['a'] = {'type': 'choice', 'instructions': ask, 'criteria': {
        'shown': f'Yes: the facts above already show “{intent}”.',
        'not_shown': f'No: the facts above do not show all of “{intent}” yet.' if name in ('a2_every_part', 'a4_both')
                     else f'No: the facts above do not show “{intent}” yet.'}}
    return sent


def orders(request: dict) -> list[dict]:
    q = list(request['questions']['q']['criteria'])
    out = []
    for qo in itertools.permutations(q):
        for ao in (('shown', 'not_shown'), ('not_shown', 'shown')):
            sent = copy.deepcopy(request)
            sent['questions']['q']['criteria'] = {k: request['questions']['q']['criteria'][k] for k in qo}
            sent['questions']['a']['criteria'] = {k: request['questions']['a']['criteria'][k] for k in ao}
            out.append(sent)
    while len(out) < 12:
        out = out + out
    return out[:12]


NAMES = ('a1_plain', 'a2_every_part', 'a3_main_hand', 'a4_both')


async def main() -> None:
    goal = json.loads((RUN / 'launcher-owner.json').read_text())['goal']
    rows = [json.loads(line) for line in (RUN / 'trace.jsonl').open()]
    frames = [r for r in rows if r['event'] == 'call_supervision_request' and r.get('purpose') == 'running_frame']
    if ONLY:
        frames = [r for r in frames if str(r['seq']) in ONLY]
    probe = BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal), decision_client=None,
                                     trace=SimpleNamespace(emit=lambda *a, **k: None)))
    jobs, meta = [], {}
    for frame in frames:
        ci, execution = frame['compiler_input'], frame['frame_input']['execution']
        rebuilt = probe._question(frame['tool_call_id'], SimpleNamespace(name=frame['tool']), ci['params'], execution,
                                  idle_s=frame['idle_s'], purpose='running_frame',
                                  supervision_context=ci['supervision_context'], options=ci['options'],
                                  decision_context=ci['decision_context'])
        rebuilt.pop('_request_chars', None)
        rebuilt.pop('_request_chars_before', None)
        if json.dumps(rebuilt, ensure_ascii=False) != json.dumps(frame['request'], ensure_ascii=False):
            raise SystemExit(f"rebuild differs at {frame['seq']}")
        intent = str(ci['supervision_context'].get('intent') or ci['supervision_context'].get('node_description') or '')
        meta[frame['seq']] = {'tool': frame['tool'], 'intent': intent, 'label': label(intent, execution.get('facts') or {})}
        if not intent or 'd' in rebuilt['questions']:
            continue
        for name in NAMES:
            for index, sent in enumerate(orders(variant(rebuilt, name, intent))):
                jobs.append((frame['seq'], name, index, sent))
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
                    raw = answer.raw
                    answers = (raw or {}).get('answers') or {}
                    a = str((answers.get('a') or {}).get('choice'))
                    q = str((answers.get('q') or {}).get('choice'))
                except Exception as exc:
                    raw, a, q = {'error': type(exc).__name__}, 'unknown', 'unknown'
                a = a if a in ('shown', 'not_shown') else 'unknown'
                final = 'next_step' if a == 'shown' else (q if q in request['questions']['q']['criteria'] else 'unknown')
                tally[(seq, name, 'a')][a] += 1
                tally[(seq, name, 'final')][final] += 1
                out.write(json.dumps({'seq': seq, 'variant': name, 'order': index, 'a': a, 'q': q, 'final': final,
                                      'request': request, 'response': raw}, ensure_ascii=False) + '\n')
                out.flush()
        finally:
            await client.close()

    await asyncio.gather(*(worker() for _ in range(WORKERS)))
    out.close()
    summary = {'source': str(RUN), 'code': '4b83331', 'frames': []}
    board = defaultdict(lambda: defaultdict(Counter))
    for seq, info in meta.items():
        row = dict(info, seq=seq)
        for name in NAMES:
            if (seq, name, 'a') in tally:
                row[name] = {'a': dict(tally[(seq, name, 'a')]), 'final': dict(tally[(seq, name, 'final')])}
                for k, v in tally[(seq, name, 'a')].items():
                    board[name][row['label']][k] += v
        summary['frames'].append(row)
    OUT.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    for name, by in board.items():
        print(name, {str(k): dict(v) for k, v in by.items()})


asyncio.run(main())
