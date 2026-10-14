"""Production return_place_question on the step-3 live interruptions, plus place controls."""
import asyncio, json, statistics, sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq

RUN = '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step3-current'

async def main():
    rows = [json.loads(l) for l in open(RUN + '/trace.jsonl') if '"default_result_frame"' in l and '"call_supervision_request"' in l]
    c = DecisionClient()
    for r in rows:
        ci = r.get('compiler_input') or {}
        hist = [h for h in (ci.get('decision_context') or {}).get('interrupted_history') or []
                if (((h.get('result') or {}).get('metrics') or {}).get('direct_call_handoff') or {}).get('facts', {}).get('reason') == 'decision_default_tool']
        if len(hist) < 2:
            print('seq', r['seq'], 'interruptions', len(hist), '-> not asked')
            continue
        intent = (ci.get('supervision_context') or {}).get('intent', '')
        before, now = hist[-2]['body']['position'], hist[-1]['body']['position']
        for name, n in (('real', now), ('8 blocks away', [now[0] + 8, now[1], now[2]]), ('50 blocks away', [now[0] + 40, 64.0, now[2] - 30])):
            q0 = rq.return_place_question(work_call='collect_resource(count=8, item="logs")', intent=intent, before=before, now=n)
            ps, ch = [], {}
            for flip in (0, 1):
                q = json.loads(json.dumps(q0))
                if flip:
                    cr = q['questions']['r']['criteria']; q['questions']['r']['criteria'] = {k: cr[k] for k in reversed(list(cr))}
                for _ in range(3):
                    ans = await c.ask(q)
                    if ans.raw:
                        a = ans.raw['answers']['r']; ps.append(a['probabilities']['same']); ch[a['choice']] = ch.get(a['choice'], 0) + 1
                        break
            print(f"seq {r['seq']} interruptions={len(hist)} {name:15s} before={before} now={n} P(same)={statistics.mean(ps):.2f} {ch}")
    await c.close()

asyncio.run(main())
