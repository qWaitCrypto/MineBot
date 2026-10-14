"""Progress-line wording vs the separate intent question, on two live runs.

Finding (why-shown-probe3): the line "inventory changed: oak_log +3" makes the
4B Decision model answer "shown" for an unmet intent; removing only that line
fixes it.  Here each variant rewrites only the progress line of the recorded
request (code ae4318a / 4b83331 + new structure) and asks question a in both
option orders.  Labels are the reviewer's reading; bench only.
"""
import asyncio, copy, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path
ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.decision_client import DecisionClient
from tests.unit.reflex_expected import intent_question_while_running, main_hand_beside_inventory

LOGS = ROOT / 'logs'
OUT = LOGS / 'step2-progress-wording-trial-20261014.jsonl'
RUNS = {'new': 'reflex-always-on-20261014-step2-intent-question',
        'old': 'reflex-always-on-20261014-step2-current-4b83331'}
NAMES = {'原木': '_log', '木板': 'oak_planks', '煤': 'coal', '工作台': 'crafting_table', '石镐': 'stone_pickaxe',
         '木镐': 'wooden_pickaxe', '圆石': 'cobblestone', '木棍': 'stick'}
DELTA = re.compile(r' inventory changed: (.*)\.$')


def label(intent, inventory, hand):
    inv = {}
    for part in (inventory or '').split(', '):
        bits = part.rsplit(' ', 1)
        if len(bits) == 2 and bits[1].lstrip('-').isdigit():
            inv[bits[0]] = int(bits[1])
    def have(item):
        if item == '_log':
            return sum(v for k, v in inv.items() if k.endswith('_log'))
        return inv.get(item, 0)
    def item_of(text):
        for zh, item in NAMES.items():
            if zh in text:
                return item
        m = re.search(r'[a-z]+_[a-z_]+|coal|stick', text)
        return m.group(0) if m else None
    item = item_of(intent)
    if not item:
        return None
    m = re.search(r'(\d+)\s*(?:个|把|块)', intent)
    need = int(m.group(1)) if m else 1
    ok = have(item) >= need
    if '装备' in intent or '主手' in intent:
        ok = ok and hand == item
    return 'met' if ok else 'unmet'


def variant(req, name):
    r = copy.deepcopy(req)
    p = r['state'].get('progress')
    if not p:
        return r
    m = DELTA.search(p)
    if name == 'p0_current' or not m:
        return r
    head = p[:m.start()] + '.' if not p[:m.start()].endswith('.') else p[:m.start()]
    if name == 'p1_no_delta':
        r['state']['progress'] = head
    elif name == 'p2_delta_in_inventory':
        r['state']['progress'] = head
        r['state']['inventory'] = r['state']['inventory'] + f' (changed during this call: {m.group(1)})'
    elif name == 'p3_delta_as_count_change':
        r['state']['progress'] = head + f' Items gained or lost during this call: {m.group(1)}; the totals are in the inventory.'
    return r


def to_new(req, intent):
    """Old-run requests (4b83331) -> current structure, as the tests define it."""
    body = req['state'].get('body', '')
    marker = ' The main hand holds '
    if marker in body:
        held = body[body.index(marker) + len(marker):-1]
        req = copy.deepcopy(req)
        req['state']['body'] = body[:body.index(marker)]
        req = main_hand_beside_inventory(req, {'selected_item': '' if held == 'nothing' else held})
    return intent_question_while_running(req, intent)


async def main():
    frames = []
    for tag, run in RUNS.items():
        rows = [json.loads(l) for l in (LOGS / run / 'trace.jsonl').open()
                if '"call_supervision_request"' in l and '"running_frame"' in l]
        per_call = defaultdict(list)
        for r in rows:
            ctx = (r.get('compiler_input') or {}).get('supervision_context') or {}
            intent = str(ctx.get('intent') or ctx.get('node_description') or '')
            if not intent or ctx.get('is_reflex_action') or ctx.get('is_life_action'):
                continue
            req = r['request'] if tag == 'new' else to_new(r['request'], intent)
            if 'a' not in req['questions']:
                continue
            lab = label(intent, req['state'].get('inventory'), req['state'].get('main_hand'))
            if lab is None or not DELTA.search(req['state'].get('progress', '')):
                continue
            per_call[r['tool_call_id']].append((tag, r['seq'], lab, req))
        for items in per_call.values():
            step = max(1, len(items) // 12)
            frames += items[::step][:12]
    jobs = []
    for tag, seq, lab, req in frames:
        for name in ('p0_current', 'p1_no_delta', 'p2_delta_in_inventory', 'p3_delta_as_count_change'):
            sent = variant(req, name)
            for flip in (False, True):
                s = copy.deepcopy(sent)
                if flip:
                    a = s['questions']['a']['criteria']
                    s['questions']['a']['criteria'] = {k: a[k] for k in reversed(list(a))}
                jobs.append((tag, seq, lab, name, s))
    q = asyncio.Queue()
    for j in jobs:
        q.put_nowait(j)
    board = defaultdict(Counter)
    per_frame = defaultdict(Counter)
    out = OUT.open('w')

    async def worker():
        c = DecisionClient()
        try:
            while True:
                try:
                    tag, seq, lab, name, req = q.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    a = (await c.ask(req)).raw['answers']['a']['choice']
                except Exception:
                    a = 'unknown'
                board[(name, lab)][a] += 1
                per_frame[(tag, seq, lab, name)][a] += 1
                out.write(json.dumps({'run': tag, 'seq': seq, 'label': lab, 'variant': name, 'a': a,
                                      'request': req}, ensure_ascii=False) + '\n')
        finally:
            await c.close()
    await asyncio.gather(*(worker() for _ in range(4)))
    out.close()
    print('frames', len(frames), Counter(f[2] for f in frames))
    for key in sorted(board):
        print(key, dict(board[key]))
    wrong = defaultdict(list)
    for (tag, seq, lab, name), c in per_frame.items():
        bad = c.get('shown' if lab == 'unmet' else 'not_shown', 0)
        if bad:
            wrong[name].append((tag, seq, lab, bad))
    for name, items in wrong.items():
        print(name, 'frames with errors', len(items), items[:12])
    OUT.with_suffix('.summary.json').write_text(json.dumps(
        {f'{k[0]}|{k[1]}': dict(v) for k, v in board.items()}, ensure_ascii=False, indent=1))


asyncio.run(main())
