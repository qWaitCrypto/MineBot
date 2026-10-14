"""Separate 'is the intent shown?' request with only the facts it needs, and
Agent intents written with the Body's item ids (English).

Variants per running frame (two live runs, all frames with an intent):
  current   question a inside the full frame request (as live)
  min_zh    own request: intent (as the Agent wrote it) + inventory + main hand + position
  min_en    the same, intent rewritten in English with item ids (how the new
            Agent contract asks the Agent to write it)
Both option orders of a.  Metrics: per-frame errors, and per call whether any
frame of unmet intent was answered 'shown' (a false stop in a live run).
Labels and English intents are the reviewer's; bench only.
"""
import asyncio, copy, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path
ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.decision_client import DecisionClient
from tests.unit.reflex_expected import intent_question_while_running, main_hand_beside_inventory

LOGS = ROOT / 'logs'
OUT = LOGS / 'step2-done-request-trial-20261014.jsonl'
RUNS = {'old': 'reflex-always-on-20261014-step2-current-4b83331',
        'new': 'reflex-always-on-20261014-step2-intent-question'}
EN = {
    '背包里有至少 12 个 coal': 'at least 12 coal in the inventory',
    '背包里有至少 3 个 cobblestone': 'at least 3 cobblestone in the inventory',
    '背包里至少有 5 个原木': 'at least 5 oak_log in the inventory',
    '背包里有 1 个 crafting_table': 'at least 1 crafting_table in the inventory',
    '背包里有 1 把 stone_pickaxe 并已装备到主手': 'a stone_pickaxe in the inventory and held in the main hand',
    '背包里有 1 把 wooden_pickaxe 并已装备到主手': 'a wooden_pickaxe in the inventory and held in the main hand',
    '背包里有至少 10 个 oak_planks': 'at least 10 oak_planks in the inventory',
    '背包里有至少 4 个 stick': 'at least 4 stick in the inventory',
    '背包里有至少 8 个 oak_planks': 'at least 8 oak_planks in the inventory',
    'wooden_pickaxe 已装备到主手': 'wooden_pickaxe held in the main hand',
    '回到地面开阔处，以便放置工作台': 'standing on open ground at the surface',
    '背包里累计2个原木，用来做工作台': 'at least 2 oak_log in the inventory',
    '背包里累计3个圆石': 'at least 3 cobblestone in the inventory',
    '背包里累计3块圆石（只挖地表裸露岩石）': 'at least 3 cobblestone in the inventory',
    '背包里累计3块圆石，用于合成石镐': 'at least 3 cobblestone in the inventory',
    '背包里累计3块圆石，用于合成石镐；绕开下方水腔': 'at least 3 cobblestone in the inventory',
    '背包里累计4个原木': 'at least 4 oak_log in the inventory',
    '背包里累计拥有12块煤': 'at least 12 coal in the inventory',
    '背包里至少有2个原木，用于补做工作台': 'at least 2 oak_log in the inventory',
    '拥有并已装备石镐': 'a stone_pickaxe in the inventory and held in the main hand',
    '拥有并拿着木镐': 'a wooden_pickaxe in the inventory and held in the main hand',
    '背包里拥有12块木板': 'at least 12 oak_planks in the inventory',
    '背包里拥有4块木板': 'at least 4 oak_planks in the inventory',
    '背包里拥有4根木棍': 'at least 4 stick in the inventory',
    '背包里拥有一个工作台': 'at least 1 crafting_table in the inventory',
    '背包里拥有工作台': 'at least 1 crafting_table in the inventory',
    '从水腔竖井垫方块升到地表 y≈73，彻底脱离溺水风险': 'standing at the surface at y 73 or higher',
    '从积水石室挖竖井到达地表 y≈73，脱离溺水风险': 'standing at the surface at y 73 or higher',
    '从积水石腔里挖竖井上升到地表（y≈71），顺带拿到圆石': 'standing at the surface at y 71 or higher',
    '把石镐装备到主手，供采煤使用': 'stone_pickaxe held in the main hand',
    '挖掉干燥井壁(176,63,149)的石头，拿到1块圆石': 'the stone at [176, 63, 149] mined and its cobblestone in the inventory',
    '走到石质海岸的裸露岩石旁': 'standing next to the bare rock on the stony shore',
}
ITEM = re.compile(r'at least (\d+) ([a-z_]+) in the inventory')


def label(en, state):
    inv = {}
    for part in (state.get('inventory') or '').split(', '):
        bits = part.rsplit(' ', 1)
        if len(bits) == 2 and bits[1].lstrip('-').isdigit():
            inv[bits[0]] = int(bits[1])
    hand = state.get('main_hand')
    m = ITEM.fullmatch(en)
    if m:
        return 'met' if inv.get(m.group(2), 0) >= int(m.group(1)) else 'unmet'
    m = re.fullmatch(r'an? ([a-z_]+) in the inventory and held in the main hand', en)
    if m:
        return 'met' if inv.get(m.group(1), 0) >= 1 and hand == m.group(1) else 'unmet'
    m = re.fullmatch(r'([a-z_]+) held in the main hand', en)
    if m:
        return 'met' if hand == m.group(1) else 'unmet'
    m = re.fullmatch(r'standing at the surface at y (\d+) or higher', en)
    pos = re.search(r'now at \[([-\d.]+), ([-\d.]+), ([-\d.]+)\]', state.get('position') or '')
    if m and pos:
        return 'met' if float(pos.group(2)) >= int(m.group(1)) else 'unmet'
    return None


def to_new(req, intent):
    body = req['state'].get('body', '')
    marker = ' The main hand holds '
    if marker in body:
        held = body[body.index(marker) + len(marker):-1]
        req = copy.deepcopy(req)
        req['state']['body'] = body[:body.index(marker)]
        req = main_hand_beside_inventory(req, {'selected_item': '' if held == 'nothing' else held})
    return intent_question_while_running(req, intent)


def done_request(state, intent):
    small = {'agent_intent': f'The Agent wants the following result: “{intent}”.'}
    for key in ('inventory', 'main_hand', 'position'):
        if state.get(key):
            small[key] = state[key]
    return {'state': small, 'questions': {'a': {'type': 'choice', 'instructions': (
        'Compare each part of the result the Agent wants with the inventory and Body facts above. '
        'Do the facts already show every part of it?'), 'criteria': {
        'shown': f'Yes: the facts above already show “{intent}”.',
        'not_shown': f'No: the facts above do not show all of “{intent}” yet.'}}}}


async def main():
    frames = []
    for tag, run in RUNS.items():
        for line in (LOGS / run / 'trace.jsonl').open():
            if '"call_supervision_request"' not in line or '"running_frame"' not in line:
                continue
            r = json.loads(line)
            ctx = (r.get('compiler_input') or {}).get('supervision_context') or {}
            intent = str(ctx.get('intent') or ctx.get('node_description') or '')
            if not intent or ctx.get('is_reflex_action') or ctx.get('is_life_action'):
                continue
            full = r['request'] if tag == 'new' else to_new(r['request'], intent)
            if 'a' not in full['questions']:
                continue
            en = EN[intent]
            frames.append({'run': tag, 'seq': r['seq'], 'call': r['tool_call_id'], 'intent': intent, 'en': en,
                           'label': label(en, full['state']), 'full': full})
    jobs = []
    for f in frames:
        for name, req in (('current', f['full']), ('min_zh', done_request(f['full']['state'], f['intent'])),
                          ('min_en', done_request(f['full']['state'], f['en']))):
            for flip in (0, 1):
                s = copy.deepcopy(req)
                if flip:
                    a = s['questions']['a']['criteria']
                    s['questions']['a']['criteria'] = {k: a[k] for k in reversed(list(a))}
                jobs.append((f, name, flip, s))
    q = asyncio.Queue()
    for j in jobs:
        q.put_nowait(j)
    answers = {}
    out = OUT.open('w')

    async def worker():
        c = DecisionClient()
        try:
            while True:
                try:
                    f, name, flip, req = q.get_nowait()
                except asyncio.QueueEmpty:
                    return
                a = 'unknown'
                for _ in range(2):
                    try:
                        ans = await c.ask(req)
                        a = ans.raw['answers']['a']['choice']
                        break
                    except Exception:
                        continue
                answers[(f['run'], f['seq'], name, flip)] = a
                out.write(json.dumps({'run': f['run'], 'seq': f['seq'], 'variant': name, 'flip': flip, 'a': a,
                                      'label': f['label'], 'request': req}, ensure_ascii=False) + '\n')
        finally:
            await c.close()
    await asyncio.gather(*(worker() for _ in range(4)))
    out.close()
    summary = {}
    for name in ('current', 'min_zh', 'min_en'):
        board = defaultdict(Counter)
        calls = defaultdict(lambda: {'false_stop': [False, False], 'label_seen': set()})
        for f in frames:
            for flip in (0, 1):
                a = answers[(f['run'], f['seq'], name, flip)]
                board[str(f['label'])][a] += 1
                key = (f['run'], f['call'])
                calls[key]['label_seen'].add(f['label'])
                if f['label'] == 'unmet' and a == 'shown':
                    calls[key]['false_stop'][flip] = True
        labelled_calls = [k for k, v in calls.items() if 'unmet' in v['label_seen']]
        stops = [sum(calls[k]['false_stop'][flip] for k in labelled_calls) for flip in (0, 1)]
        summary[name] = {'frames': {k: dict(v) for k, v in board.items()},
                         'calls_with_unmet_frames': len(labelled_calls), 'calls_falsely_stopped_by_order': stops,
                         'falsely_stopped_calls': sorted({f"{k[0]}:{k[1]}" for k in labelled_calls
                                                          if any(calls[k]['false_stop'])})}
        print(name, json.dumps(summary[name], ensure_ascii=False))
    unl = Counter((f['intent'][:20], answers[(f['run'], f['seq'], 'min_en', 0)]) for f in frames if f['label'] is None)
    print('unlabelled (min_en, order 0):', dict(unl))
    OUT.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))


asyncio.run(main())
