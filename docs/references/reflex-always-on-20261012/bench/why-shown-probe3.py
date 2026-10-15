"""Why does Decision call an unmet intent 'shown'?  Controlled probes on live frames.

Base requests are the exact asked frames from the ae4318a live run.  Each probe
changes one thing; we record P(shown) (native option probability) averaged
over 4 orders (a-order x q-order) and the choice counts.
"""
import asyncio, copy, itertools, json, re, statistics, sys
from pathlib import Path
ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.decision_client import DecisionClient

RUN = ROOT / 'logs/reflex-always-on-20261014-step2-intent-question'
OUT = ROOT / 'logs/why-shown-probe3-20261014.jsonl'
STOPS = (1204, 12522, 13991)


def asked_for(rows, stop_seq):
    stop = next(r for r in rows if r['seq'] == stop_seq)
    cid = stop['tool_call_id']
    res = [r for r in rows if r.get('event') == 'call_supervision_result' and r.get('tool_call_id') == cid and r['seq'] < stop_seq][-1]
    return [r for r in rows if r.get('event') == 'call_supervision_request' and r.get('tool_call_id') == cid and r['seq'] < res['seq']][-1]


def set_count(req, item, n):
    r = copy.deepcopy(req)
    inv = r['state']['inventory']
    if re.search(rf'\b{item} \d+', inv):
        inv = re.sub(rf'\b{item} \d+', f'{item} {n}', inv)
    else:
        inv = (inv + f', {item} {n}') if inv and inv != 'empty' else f'{item} {n}'
    r['state']['inventory'] = inv
    return r


def set_intent(req, old_intent, new_intent):
    return json.loads(json.dumps(req, ensure_ascii=False).replace(old_intent, new_intent))


def drop(req, *keys):
    r = copy.deepcopy(req)
    for k in keys:
        r['state'].pop(k, None)
    return r


def only(req, *keys):
    r = copy.deepcopy(req)
    r['state'] = {k: v for k, v in r['state'].items() if k in keys}
    return r


def a_only(req):
    r = copy.deepcopy(req)
    r['questions'] = {'a': r['questions']['a']}
    return r


def orders(req):
    out = []
    qkeys = list(req['questions'].get('q', {}).get('criteria', {})) or [None]
    for qo in (itertools.permutations(qkeys) if qkeys != [None] else [None]):
        for ao in (('shown', 'not_shown'), ('not_shown', 'shown')):
            r = copy.deepcopy(req)
            if qo:
                r['questions']['q']['criteria'] = {k: req['questions']['q']['criteria'][k] for k in qo}
            r['questions']['a']['criteria'] = {k: req['questions']['a']['criteria'][k] for k in ao}
            out.append(r)
    return out[:4] if len(out) >= 4 else (out * 4)[:4]


async def main():
    rows = [json.loads(l) for l in (RUN / 'trace.jsonl').open()]
    probes = []
    base = {s: asked_for(rows, s)['request'] for s in STOPS}
    intent_of = {s: re.search('“(.*)”', base[s]['state']['agent_intent']).group(1) for s in STOPS}
    b = base[1204]; it = intent_of[1204]
    en = set_intent(b, it, 'at least 4 oak_log in the inventory')
    keys = [k for k in en['state'] if k not in ('agent_intent', 'inventory', 'main_hand')]
    print('state keys', list(en['state']), flush=True)
    for n in (3, 5):
        probes.append(('1204', f'en full n={n}', set_count(en, 'oak_log', n)))
        probes.append(('1204', f'en minimal n={n}', set_count(only(en, 'agent_intent', 'inventory', 'main_hand'), 'oak_log', n)))
        for k in keys:
            probes.append(('1204', f'en full minus {k} n={n}', set_count(drop(en, k), 'oak_log', n)))
            probes.append(('1204', f'en minimal plus {k} n={n}', set_count(only(en, 'agent_intent', 'inventory', 'main_hand', k), 'oak_log', n)))
    zid = set_intent(b, it, '背包里至少有4个oak_log')
    for n in (3, 5):
        probes.append(('1204', f'zh-with-id minimal n={n}', set_count(only(zid, 'agent_intent', 'inventory', 'main_hand'), 'oak_log', n)))
        probes.append(('1204', f'zh-original minimal, progress+work dropped n={n}', set_count(drop(b, 'progress', 'current_work'), 'oak_log', n)))
    client = DecisionClient()
    results = []
    with OUT.open('w') as sink:
        for frame, name, req in probes:
            ps, choices = [], {}
            for r in orders(req):
                try:
                    ans = await client.ask(r)
                    a = ans.raw['answers']['a']
                    ps.append(a['probabilities']['shown'])
                    choices[a['choice']] = choices.get(a['choice'], 0) + 1
                except Exception as exc:
                    choices['error'] = choices.get('error', 0) + 1
                    ans = None
                sink.write(json.dumps({'frame': frame, 'probe': name, 'request': r,
                                       'response': ans.raw if ans else None}, ensure_ascii=False) + '\n')
            row = {'frame': frame, 'probe': name, 'p_shown_mean': round(statistics.mean(ps), 3) if ps else None,
                   'p_shown_range': [round(min(ps), 3), round(max(ps), 3)] if ps else None, 'choices': choices,
                   'inventory': req['state'].get('inventory'), 'main_hand': req['state'].get('main_hand')}
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    await client.close()
    OUT.with_suffix('.summary.json').write_text(json.dumps(results, ensure_ascii=False, indent=1))


asyncio.run(main())
