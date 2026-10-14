"""Is the running protective action working? Separate small question vs the in-frame keep_going."""
import asyncio, json, statistics, sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.decision_client import DecisionClient
RUN = '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step5-threat-claude2'

def small(state, action):
    s = {'current_protective_action': action}
    for k in ('body', 'threats', 'position'):
        if state.get(k):
            s[k] = state[k]
    return {'state': s, 'questions': {'w': {'type': 'choice',
        'instructions': 'Is the current protective action keeping the Body away from harm?',
        'criteria': {'working': 'Yes: the Body is not being hurt and the threat is not right next to it.',
                     'failing': 'No: the Body is still being hurt or the threat is still right next to it.'}}}}

async def main():
    rows = [json.loads(l) for l in open(RUN + '/trace.jsonl') if '"call_supervision_request"' in l]
    frames = [r for r in rows if r.get('purpose') == 'running_frame' and r.get('tool') == 'move_away' and 600 <= r['seq'] <= 945]
    c = DecisionClient()
    for r in frames:
        st = r['request']['state']
        req = small(st, 'move_away from the Zombie (running)')
        ps = []
        for flip in (0, 1):
            q = json.loads(json.dumps(req))
            if flip:
                cr = q['questions']['w']['criteria']; q['questions']['w']['criteria'] = {k: cr[k] for k in reversed(list(cr))}
            for _ in range(3):
                a = await c.ask(q)
                if a.raw:
                    ps.append(a.raw['answers']['w']['probabilities']['working']); break
        print(r['seq'], '|', st.get('body', '')[:20], '|', st.get('threats'), '| hurt' if 'just hurt' in st.get('body', '') else '|', f'P(working)={statistics.mean(ps):.2f}')
    await c.close()
asyncio.run(main())
