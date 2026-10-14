"""Production compile of the protective re-ask on live step-5 frames (real Decision, 6 orders)."""
import asyncio, itertools, json, sys
from collections import Counter
from types import SimpleNamespace
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq
RUN = '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step5-threat-claude2'

async def main():
    goal = json.load(open(RUN + '/launcher-owner.json'))['goal']
    rows = [json.loads(l) for l in open(RUN + '/trace.jsonl') if '"call_supervision_request"' in l]
    probe = BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal), decision_client=None,
                                     trace=SimpleNamespace(emit=lambda *a, **k: None)))
    c = DecisionClient()
    for r in rows:
        if not (r.get('purpose') == 'running_frame' and r.get('tool') in ('engage_entity', 'move_away') and 595 <= r['seq'] <= 945):
            continue
        ci = r['compiler_input']; ex = r['frame_input']['execution']
        tool = SimpleNamespace(name=r['tool'])
        record = {'tool': r['tool'], 'params': ci['params'], 'supervision_context': ci['supervision_context']}
        options = probe._options_for(tool, record, execution=ex)
        req = probe._question(r['tool_call_id'], tool, ci['params'], ex, idle_s=r['idle_s'], purpose='running_frame',
                              supervision_context=ci['supervision_context'], options=options, decision_context=ci['decision_context'])
        for k in [k for k in req if k.startswith('_')]:
            req.pop(k)
        keys = list(req['questions']['q']['criteria'])
        perms = list(itertools.permutations(keys)); perms = [perms[i] for i in range(0, len(perms), max(1, len(perms) // 6))][:6]
        cnt = Counter()
        for o in perms:
            q = json.loads(json.dumps(req)); q['questions']['q']['criteria'] = {k: req['questions']['q']['criteria'][k] for k in o}
            for _ in range(3):
                a = await c.ask(q)
                if a.raw:
                    cnt[rq.frame_choice(a.raw, q['questions'])] += 1; break
        st = req['state']
        print(r['seq'], r['tool'], '|', st.get('body', '')[:20], '|', st.get('threats', '')[19:], '| options', keys, '|', dict(cnt))
    print('instruction:', req['questions']['q']['instructions'])
    print('engage text:', req['questions']['q']['criteria'].get('engage'))
    await c.close()
asyncio.run(main())
