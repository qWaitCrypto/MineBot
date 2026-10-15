"""Offline probe: why seek_player is never chosen when the profile asks for it."""
import asyncio, collections, json, sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq

LOGS = '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-'
RUNS = {'zh': 'step8-extra-claude2', 'en': 'step8-extra-claude3'}


def frames(run):
    rows = [json.loads(l) for l in open(LOGS + run + '/trace.jsonl') if l.startswith('{')]
    fx = [json.loads(l) for l in open(LOGS + run + '/fixture-events.jsonl')]
    a = next(f['ts'] for f in fx if f.get('label') == 'seek' and f['kind'] == 'scene')
    out = [r for r in rows if r.get('event') == 'call_supervision_request' and r.get('purpose') == 'idle_frame'
           and r['ts'] >= a + 5 and 'seek_player' in r['request']['questions']['q']['criteria']]
    return out[::4]


async def ask(client, request):
    for _ in range(3):
        a = await client.ask(request)
        if a.raw:
            return rq.frame_choice(a.raw, request['questions'])
    return 'unknown'


async def main():
    client = DecisionClient()
    for lang, run in RUNS.items():
        fs = frames(run)
        print(lang, 'frames', len(fs))
        print('  state:', json.dumps(fs[0]['request']['state'], ensure_ascii=False)[:600])
        print('  options:', json.dumps(fs[0]['request']['questions']['q']['criteria'], ensure_ascii=False)[:600])
        for variant in ('V0', 'first', 'reworded', 'reworded_first'):
            c = collections.Counter()
            for r in fs:
                q0 = json.loads(json.dumps(r['request']))
                crit = q0['questions']['q']['criteria']
                if variant.startswith('reworded'):
                    crit['seek_player'] = 'Go and look for Visitor nearby.'
                keys = list(crit)
                if variant.endswith('first'):
                    keys = ['seek_player'] + [k for k in keys if k != 'seek_player']
                for order in (keys, list(reversed(keys))):
                    q = json.loads(json.dumps(q0))
                    q['questions']['q']['criteria'] = {k: crit[k] for k in order}
                    c[await ask(client, q)] += 1
            print('  ', variant, dict(c))
    await client.close()

asyncio.run(main())
