"""Continuity of a running protective action: say how long it has been going.
Live frames from three runs, production compile, then only the running option's text changes."""
import asyncio, itertools, json, re, sys
from collections import Counter
from types import SimpleNamespace
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq

LOGS = '/home/qwait/MineBot-always-on/logs/'
# (run, tool, seq range, expected)
SETS = [
    ('reflex-always-on-20261014-step6-spare-claude2', 'engage_entity', (500, 1520), 'engage while health holds'),
    ('reflex-always-on-20261014-step5-threat-claude2', 'move_away', (895, 940), 'engage (adjacent, being hit)'),
    ('reflex-always-on-20261014-step5-threat-claude2', 'move_away', (655, 880), 'move_away (escape working)'),
    ('reflex-always-on-20261014-step5-threat-claude5', 'move_away', (1300, 1430), 'move_away (low health, skeleton)'),
]
VARIANTS = ('V0', 'V1', 'V2')


def running_text(name, base, choice, mob, elapsed, hand):
    if name == 'V0':
        return base
    secs = f'{elapsed:.0f} seconds' if elapsed >= 1.5 else '1 second'
    if name == 'V1':
        if choice == 'engage':
            return f'Keep fighting the {mob} in melee with the {hand} in the main hand; this fight started {secs} ago.'
        if choice == 'move_away':
            return f'Keep moving away from the {mob}; this started {secs} ago.'
        return base
    return base[:-1] + (f'; this fight started {secs} ago.' if choice == 'engage' else f'; started {secs} ago.')


async def main():
    c = DecisionClient()
    for run, tool, (lo, hi), expect in SETS:
        goal = json.load(open(LOGS + run + '/launcher-owner.json'))['goal']
        probe = BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal), decision_client=None,
                                         trace=SimpleNamespace(emit=lambda *a, **k: None)))
        frames = [json.loads(l) for l in open(LOGS + run + '/trace.jsonl')
                  if '"call_supervision_request"' in l and '"running_frame"' in l]
        frames = [r for r in frames if r.get('tool') == tool and lo <= r['seq'] <= hi][:8]
        print(f'== {run[-22:]} {tool} expect: {expect}')
        for r in frames:
            ci = r['compiler_input']; ex = r['frame_input']['execution']
            t = SimpleNamespace(name=r['tool'])
            record = {'tool': r['tool'], 'params': ci['params'], 'supervision_context': ci['supervision_context']}
            options = probe._options_for(t, record, execution=ex)
            req = probe._question(r['tool_call_id'], t, ci['params'], ex, idle_s=r['idle_s'], purpose='running_frame',
                                  supervision_context=ci['supervision_context'], options=options,
                                  decision_context=ci['decision_context'])
            for k in [k for k in req if k.startswith('_')]:
                req.pop(k)
            choice = ci['supervision_context'].get('reflex_action_choice')
            st = req['state']
            m = re.search(r'Nearest hostile mob: (\w+), (\d+)', st.get('threats', ''))
            mob = m.group(1) if m else 'mob'
            line = []
            for v in VARIANTS:
                q0 = json.loads(json.dumps(req))
                crit = q0['questions']['q']['criteria']
                if choice in crit:
                    crit[choice] = running_text(v, crit[choice], choice, mob, float(ex.get('elapsed_s') or 0), st.get('main_hand'))
                keys = list(crit)
                cnt = Counter()
                for o in list(itertools.permutations(keys))[:6]:
                    q = json.loads(json.dumps(q0)); q['questions']['q']['criteria'] = {k: crit[k] for k in o}
                    for _ in range(3):
                        a = await c.ask(q)
                        if a.raw:
                            cnt[rq.frame_choice(a.raw, q['questions'])] += 1; break
                line.append(v + ':' + ','.join(f'{k}{n}' for k, n in cnt.most_common()))
            print(f"  {r['seq']} {st.get('body','')[:18]} | {st.get('threats','')[19:]} | t={ex.get('elapsed_s')} | {' '.join(line)}")
    await c.close()

asyncio.run(main())
