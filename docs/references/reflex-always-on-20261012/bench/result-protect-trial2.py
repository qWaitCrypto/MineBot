"""Protective result frames re-offer the other protective actions: replay live frames."""
import asyncio, itertools, json, sys
from collections import Counter
from types import SimpleNamespace
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq
VARIANT = sys.argv[1]
RUNS = {'claude4': '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step5-threat-claude4',
        'step3': '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step3-water'}

async def main():
    c = DecisionClient()
    for tag, run in RUNS.items():
        goal = json.load(open(run + '/launcher-owner.json'))['goal']
        probe = BodyLoop(SimpleNamespace(agent_context=SimpleNamespace(goal_text=goal), decision_client=None,
                                         trace=SimpleNamespace(emit=lambda *a, **k: None)))
        rows = [json.loads(l) for l in open(run + '/trace.jsonl') if '"call_supervision_request"' in l and '"default_result_frame"' in l]
        seen = 0
        for r in rows:
            ci = r.get('compiler_input') or {}
            dc = ci.get('decision_context') or {}
            facts = dc.get('body_state') or {}
            st = r['request']['state']
            fin = st.get('finished_call', '')
            choice = 'engage' if fin.startswith('engage') else 'move_away' if fin.startswith('move_away') else 'surface' if fin.startswith('go_to_surface') else ''
            if tag == 'claude4' and not (1700 <= r['seq'] <= 1830):
                continue
            if tag == 'step3' and seen >= 4:
                continue
            seen += 1
            old_opts = r['request']['questions']['q']['criteria']
            work = st.get('agent_intent', '')
            opts = rq.after_default_options(original_call='x' if 'resume' in old_opts else '', intent='', default_call=fin.split(' has ended')[0],
                                            result=dc.get('tool_result') or {}, facts=facts, protective_choice=choice)
            opts = {**({'resume': old_opts['resume']} if 'resume' in old_opts else {}), **{k: v for k, v in opts.items() if k != 'resume'}}
            # R1: the just-ended action under its own name instead of "repeat".
            prot = rq.protective_options(facts)
            if choice and choice not in prot:
                prot = {choice: rq.DEFAULT_ACTION_INTENTS.get(choice, choice), **prot}
            opts = {**{k: v for k, v in opts.items() if k in ('resume', 'stay')}, **prot,
                    'call_agent': opts['call_agent']}
            req = probe._question(r['tool_call_id'], SimpleNamespace(name=r['tool']), ci['params'], r['frame_input']['execution'],
                                  idle_s=0, purpose='default_result_frame', supervision_context={**ci['supervision_context'], 'is_reflex_action': True},
                                  options=opts, decision_context=dc)
            for k in [k for k in req if k.startswith('_')]:
                req.pop(k)
            if VARIANT == 'R2':
                req['state'] = {k: req['state'][k] for k in ('finished_call',) + rq.PROTECTION_FACTS if req['state'].get(k)}
            cnt = Counter()
            keys = list(req['questions']['q']['criteria'])
            for o in list(itertools.permutations(keys))[:6]:
                q = json.loads(json.dumps(req)); q['questions']['q']['criteria'] = {k: req['questions']['q']['criteria'][k] for k in o}
                for _ in range(3):
                    a = await c.ask(q)
                    if a.raw:
                        cnt[rq.frame_choice(a.raw, q['questions'])] += 1; break
            print(tag, r['seq'], fin[:70], '|', st.get('body', '')[:18], '|', st.get('threats', '')[19:], '| q', keys, 'p', list(req['questions'].get('p', {}).get('criteria', {})), '|', dict(cnt))
            if tag == 'claude4' and seen >= 3:
                break
    await c.close()
asyncio.run(main())
