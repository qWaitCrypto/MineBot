"""Generic item names in intents ('logs') vs inventory item ids: P(shown) sweeps."""
import asyncio, json, statistics, sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq

INTENTS = {
    'generic': 'at least 8 logs in the inventory',
    'any kind listed': 'at least 8 logs of any kind (oak_log, birch_log, spruce_log, ...) in the inventory',
    'exact id': 'at least 8 oak_log in the inventory',
}
def inv(oak, birch=0):
    parts = ['leaf_litter 5', f'oak_log {oak}'] + ([f'birch_log {birch}'] if birch else [])
    return ', '.join(parts)

async def main():
    c = DecisionClient()
    cases = []
    for name, intent in INTENTS.items():
        for n in (0, 4, 7, 8, 10):
            cases.append((name, f'oak_log {n}', rq.done_question({'inventory': inv(n), 'main_hand': 'leaf_litter'}, intent)))
        cases.append((name, 'oak 4 + birch 4', rq.done_question({'inventory': inv(4, 4), 'main_hand': 'leaf_litter'}, intent)))
    # generic intent + a kind summary line beside the inventory
    for n, b in ((0, 0), (4, 0), (7, 0), (8, 0), (10, 0), (4, 4)):
        state = {'inventory': inv(n, b), 'main_hand': 'leaf_litter'}
        req = rq.done_question(state, INTENTS['generic'])
        s = {}
        for k, v in req['state'].items():
            s[k] = v
            if k == 'inventory':
                s['inventory_by_kind'] = f'logs {n + b}'
        req['state'] = s
        cases.append(('generic + kind summary', f'oak {n} birch {b}', req))
    for name, label, req in cases:
        ps = []
        for flip in (0, 1):
            r = json.loads(json.dumps(req))
            if flip:
                a = r['questions']['a']['criteria']; r['questions']['a']['criteria'] = {k: a[k] for k in reversed(list(a))}
            for _ in range(3):
                ans = await c.ask(r)
                if ans.raw:
                    ps.append(ans.raw['answers']['a']['probabilities']['shown'])
                    break
        print(f'{name:24s} {label:16s} P(shown)={statistics.mean(ps):.2f}')
    await c.close()
asyncio.run(main())
