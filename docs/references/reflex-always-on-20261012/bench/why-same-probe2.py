"""Step 3: can Decision compare the last return with this interruption (same place, nothing gained)?"""
import asyncio, json, statistics, sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.decision_client import DecisionClient

RUN = '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step3-current'

def inv_text(inv):
    return ', '.join(f'{k} {v}' for k, v in inv.items()) or 'empty'

def pos_text(p):
    return '[' + ', '.join(f'{float(v):.1f}' for v in p) + ']'

def delta_text(prev, now):
    a, b = prev.get('inventory') or {}, now.get('inventory') or {}
    parts = [f'{k} {b.get(k, 0) - a.get(k, 0):+d}' for k in sorted(set(a) | set(b)) if b.get(k, 0) != a.get(k, 0)]
    return ', '.join(parts) if parts else 'nothing'


def request(intent, work, prev, now):
    return {'state': {
        'agent_intent': f'The earlier work was for: “{intent}”.',
        'last_return': (f'Last time the Body went back to {work}, it was interrupted again to swim up to air. '
                        f'Before going back it was at {pos_text(prev["position"])} with inventory {inv_text(prev["inventory"])}.'),
        'this_interruption': (f'Now it has been interrupted to swim up to air at {pos_text(now["position"])} '
                              f'with inventory {inv_text(now["inventory"])}.'),
        'gained_since_going_back': delta_text(prev, now),
    }, 'questions': {'s': {'type': 'choice',
        'instructions': 'Compare the last return with this interruption. Did going back end the same way: '
                        'stopped by the same danger at about the same place, with nothing gained?',
        'criteria': {'same': 'Yes: going back last time was stopped the same way, at about the same place, and gained nothing.',
                     'different': 'No: going back last time gained something, or it was stopped somewhere else.'}}}}

async def main():
    rows = [json.loads(l) for l in open(RUN + '/trace.jsonl') if '"default_result_frame"' in l and '"call_supervision_request"' in l]
    c = DecisionClient()
    for r in rows:
        ci = r.get('compiler_input') or {}
        hist = (ci.get('decision_context') or {}).get('interrupted_history') or []
        if len(hist) < 2 or not all(isinstance(h.get('body', {}).get('inventory'), dict) for h in hist[-2:]):
            continue
        intent = (ci.get('supervision_context') or {}).get('intent', '')
        work = 'collect_resource(count=8, item="logs")'
        prev, now = hist[-2]['body'], hist[-1]['body']
        cases = [('real (expect same)', prev, now),
                 ('control: gained oak_log 3 (expect different)', prev, {**now, 'inventory': {**now['inventory'], 'oak_log': 3}}),
                 ('control: far away (expect different)', prev, {**now, 'position': [now['position'][0] + 40, 64.0, now['position'][2] - 35]})]
        for name, a, b in cases:
            req = request(intent, work, a, b)
            ps, ch = [], {}
            for flip in (0, 1):
                q = json.loads(json.dumps(req))
                if flip:
                    cr = q['questions']['s']['criteria']; q['questions']['s']['criteria'] = {k: cr[k] for k in reversed(list(cr))}
                for _ in range(3):
                    ans = await c.ask(q)
                    if ans.raw:
                        s = ans.raw['answers']['s']; ps.append(s['probabilities']['same']); ch[s['choice']] = ch.get(s['choice'], 0) + 1
                        break
            print(f"seq {r['seq']} interruptions={len(hist)} {name:46s} P(same)={statistics.mean(ps):.2f} {ch}")
    await c.close()

asyncio.run(main())
