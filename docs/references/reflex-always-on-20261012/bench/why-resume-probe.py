"""Why does Decision keep choosing 'resume' after the same interruption 5 times?
Controlled probes on the step3-current protective result frames (6db65ec)."""
import asyncio, copy, itertools, json, statistics, sys
from pathlib import Path
ROOT = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(ROOT))
from minebot.app.decision_client import DecisionClient

RUN = ROOT / 'logs/reflex-always-on-20261014-step3-current'
OUT = ROOT / 'logs/why-resume-probe-20261014.jsonl'
FRAMES = (980, 1053, 1141, 1286, 1506)


def inventory_at(line):
    return line.split('Inventory at interruption: ')[1].split('. Position')[0] if 'Inventory at interruption: ' in line else '?'


def summary(req):
    lines = req['state'].get('interruptions_of_the_original_work') or []
    if not lines:
        return req
    r = copy.deepcopy(req)
    invs = [inventory_at(l) for l in lines]
    same = all(i == invs[0] for i in invs)
    r['state']['interruptions_of_the_original_work'] = (
        f"The earlier work has been interrupted {len(lines)} times to swim up to air. "
        + (f"The inventory was the same each time: {invs[0]}." if same
           else "The inventory at each interruption: " + "; ".join(invs) + "."))
    return r


def minimal(req):
    r = copy.deepcopy(req)
    r['state'] = {k: v for k, v in r['state'].items()
                  if k in ('agent_intent', 'inventory', 'finished_call', 'interruptions_of_the_original_work')}
    return r


def separate(req):
    r = summary(req)
    if 'interruptions_of_the_original_work' not in r['state']:
        return None
    r['questions']['g'] = {'type': 'choice',
        'instructions': 'Read the interruptions above. Did going back to the earlier work gain anything before it was interrupted again?',
        'criteria': {'gained': 'Yes: the earlier work made progress between interruptions.',
                     'nothing': 'No: each time the Body went back, it was interrupted again with nothing gained.'}}
    return r


def orders(req):
    keys = list(req['questions']['q']['criteria'])
    out = []
    for o in itertools.permutations(keys):
        r = copy.deepcopy(req)
        r['questions']['q']['criteria'] = {k: req['questions']['q']['criteria'][k] for k in o}
        out.append(r)
    return out  # 6 orders for 3 options


async def main():
    rows = [json.loads(l) for l in (RUN / 'trace.jsonl').open() if '"default_result_frame"' in l and '"call_supervision_request"' in l]
    base = {r['seq']: r['request'] for r in rows if r['seq'] in FRAMES}
    client = DecisionClient()
    with OUT.open('w') as sink:
        for seq in FRAMES:
            b = base[seq]
            n = len(b['state'].get('interruptions_of_the_original_work') or [])
            for name, req in (('original', b), ('minimal', minimal(b)), ('summary', summary(b)),
                              ('summary minimal', minimal(summary(b))), ('separate question', separate(b))):
                if req is None:
                    continue
                counts, p_call, g = {}, [], {}
                for r in orders(req):
                    ans = await client.ask(r)
                    if not ans.raw:
                        ans = await client.ask(r)
                    if not ans.raw:
                        counts['unknown'] = counts.get('unknown', 0) + 1
                        continue
                    q = ans.raw['answers']['q']
                    counts[q['choice']] = counts.get(q['choice'], 0) + 1
                    p_call.append(q['probabilities'].get('call_agent', 0))
                    if 'g' in ans.raw['answers']:
                        c = ans.raw['answers']['g']['choice']; g[c] = g.get(c, 0) + 1
                    sink.write(json.dumps({'seq': seq, 'probe': name, 'request': r, 'response': ans.raw}, ensure_ascii=False) + '\n')
                print(json.dumps({'seq': seq, 'interruptions': n, 'probe': name, 'q': counts,
                                  'p_call_agent': round(statistics.mean(p_call), 3), 'g': g or None}, ensure_ascii=False), flush=True)
    await client.close()


asyncio.run(main())
