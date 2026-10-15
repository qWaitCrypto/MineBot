"""Offline trial: how a running life action is offered in its own running frame.

V0 (live): the running action appears only as "Keep doing the current life activity:
<raw call>" (its own named option is removed).  V1: the running action is offered under
its own named option, first, and keep_going is gone (as protection already does).
Real running frames of life actions from three live runs; recorded requests in live key
order; two option orders (as recorded / reversed) per frame.
"""
import asyncio, collections, json, re, sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq

LOGS = '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-'
RUNS = ['step8-extra-claude2', 'step8-extra-claude1', 'step7-life-claude2']
LIFE = {'look_player', 'approach_player', 'follow_player', 'go_home', 'seek_player'}


def own_text(choice, state, crit):
    m = re.search(r'Player (\S+) is', state.get('players', ''))
    name = m.group(1) if m else 'the nearby player'
    if choice == 'look_player':
        return f'Look toward {name} at the observed position.'
    if choice == 'approach_player':
        return f'Walk to {name} at the observed position.'
    if choice == 'follow_player':
        return f'Follow {name} as a moving player.'
    if choice == 'go_home':
        m = re.search(r'home position (\[[^\]]+\])', json.dumps(crit))
        return f'Walk to the home position {m.group(1) if m else ""}.'
    return None


def frames():
    out = []
    for run in RUNS:
        rows = [json.loads(l) for l in open(LOGS + run + '/trace.jsonl') if l.startswith('{')]
        reqs = {}
        for r in rows:
            if r.get('event') == 'call_supervision_request' and r.get('purpose') == 'running_frame':
                ctx = (r.get('compiler_input') or {}).get('supervision_context') or {}
                if ctx.get('is_life_action'):
                    reqs[(r.get('tool_call_id'), r.get('fact_revision'))] = (r, ctx.get('reflex_action_choice'))
            elif r.get('event') == 'call_supervision_result' and r.get('purpose') == 'running_frame':
                hit = reqs.get((r.get('tool_call_id'), r.get('fact_revision')))
                if hit and hit[1] in LIFE:
                    out.append({'run': run, 'seq': hit[0]['seq'], 'request': hit[0]['request'],
                                'running': hit[1], 'live': r.get('choice')})
    return out


async def ask(client, request):
    for _ in range(3):
        a = await client.ask(request)
        if a.raw:
            return rq.frame_choice(a.raw, request['questions'])
    return 'unknown'


def classify(choice, running, variant):
    if choice == 'keep_going' or (variant == 'V1' and choice == running):
        return 'continue'
    if choice in LIFE:
        return 'switch'
    return choice


async def main():
    fs = frames()
    print('life running frames:', len(fs), collections.Counter((f['run'], f['running']) for f in fs))
    client = DecisionClient()
    agree = 0
    for f in fs:
        agree += (await ask(client, json.loads(json.dumps(f['request'])))) == f['live']
    print('baseline replay agrees with live: %d/%d' % (agree, len(fs)))
    for variant in ('V0', 'V1'):
        tally = collections.defaultdict(collections.Counter)
        for f in fs:
            q0 = json.loads(json.dumps(f['request']))
            crit = q0['questions']['q']['criteria']
            if variant == 'V1':
                text = own_text(f['running'], q0['state'], crit)
                if text is None:
                    continue
                crit = {f['running']: text, **{k: v for k, v in crit.items() if k != 'keep_going'}}
            for order in (list(crit), list(reversed(list(crit)))):
                q = json.loads(json.dumps(q0))
                q['questions']['q']['criteria'] = {k: crit[k] for k in order}
                c = await ask(client, q)
                tally[(f['run'][-14:], f['running'])][classify(c, f['running'], variant)] += 1
        print(variant)
        for k, v in sorted(tally.items()):
            print('   ', k, dict(v))
    await client.close()

asyncio.run(main())
