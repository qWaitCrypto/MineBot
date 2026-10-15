"""Idle/life frames: main hand moved from the body sentence to its own key.
Old = recorded request; new = the same request with only that move. 12 orders."""
import asyncio, copy, itertools, json, random, sys
from collections import Counter, defaultdict
from pathlib import Path
ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq
from tests.unit.reflex_expected import main_hand_beside_inventory

RUNS = ['reflex-always-on-20261014-life-30-final-v2', 'reflex-always-on-20261014-player-handler-fixed']
OUT = ROOT / 'logs/step2-idle-mainhand-regression-20261014.jsonl'
MARK = ' The main hand holds '

def moved(request):
    body = request['state'].get('body', '')
    if MARK not in body:
        return None
    held = body[body.index(MARK) + len(MARK):-1]
    sent = copy.deepcopy(request)
    sent['state']['body'] = body[:body.index(MARK)]
    return main_hand_beside_inventory(sent, {'selected_item': '' if held == 'nothing' else held})

def orders(request):
    keys = list(request['questions']['q']['criteria'])
    perms = list(itertools.permutations(keys))
    perms = perms * 2 if len(perms) <= 6 else perms[::max(1, len(perms) // 12)][:12]
    out = []
    for order in perms[:12]:
        sent = copy.deepcopy(request)
        sent['questions']['q']['criteria'] = {k: request['questions']['q']['criteria'][k] for k in order}
        out.append(sent)
    return out

async def main():
    frames = []
    for run in RUNS:
        rows = [json.loads(l) for l in open(ROOT / 'logs' / run / 'trace.jsonl') if '"call_supervision_request"' in l]
        idle = [r for r in rows if r.get('purpose') == 'idle_frame' and len(r['request']['questions']['q']['criteria']) > 1]
        seen, picked = set(), []
        for r in idle:  # one frame per distinct option set + state shape, recorded order
            sig = (tuple(r['request']['questions']['q']['criteria']), tuple(r['request']['state']), 'd' in r['request']['questions'])
            if sig not in seen and moved(r['request']):
                seen.add(sig); picked.append(r)
        frames += [(run, r) for r in picked[:20]]
    jobs = []
    for run, r in frames:
        for side, req in (('old', r['request']), ('new', moved(r['request']))):
            for i, sent in enumerate(orders(req)):
                jobs.append(((run[-24:], r['seq']), side, i, sent))
    q = asyncio.Queue()
    for j in jobs: q.put_nowait(j)
    tally = defaultdict(Counter); out = OUT.open('x')
    async def worker():
        c = DecisionClient()
        try:
            while True:
                try: key, side, i, req = q.get_nowait()
                except asyncio.QueueEmpty: return
                try:
                    a = await c.ask(req); raw = a.raw; ch = rq.frame_choice(raw, req['questions'])
                except Exception as e:
                    raw, ch = {'error': type(e).__name__}, 'unknown'
                tally[(key, side)][ch] += 1
                out.write(json.dumps({'key': key, 'side': side, 'order': i, 'choice': ch, 'request': req, 'response': raw}, ensure_ascii=False) + '\n')
        finally:
            await c.close()
    await asyncio.gather(*(worker() for _ in range(4)))
    out.close()
    changed = 0
    for run, r in frames:
        key = (run[-24:], r['seq'])
        old, new = dict(tally[(key, 'old')]), dict(tally[(key, 'new')])
        flip = max(old, key=old.get) != max(new, key=new.get)
        changed += flip
        print(key, list(r['request']['questions']['q']['criteria']), 'old', old, 'new', new, 'FLIP' if flip else '')
    print('frames', len(frames), 'majority changed', changed)

asyncio.run(main())
