"""Offline trial: the running-frame progress line when no progress event has come yet.

Real two-option running frames (keep_going / call_agent) from six live runs, recorded
requests in live key order; only state["progress"] changes. Two option orders per frame.
"""
import asyncio, collections, json, re, statistics, sys
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.decision_client import DecisionClient
from minebot.app import reflex_question as rq

LOGS = '/home/qwait/MineBot-always-on/logs/'
RUNS = ['reflex-always-on-20261014-step2-done-request', 'reflex-always-on-20261014-step6-spare-claude3',
        'reflex-always-on-20261014-step5-threat-claude7', 'reflex-always-on-20261014-step5-threat-claude8',
        'reflex-always-on-20261014-step5-threat-claude9', 'reflex-always-on-20261014-step5-threat-claude10']
OLD = 'no real progress has been observed yet.'


def variant(name, progress, elapsed):
    if not progress.startswith(OLD):
        return progress
    rest = progress[len(OLD):]
    if name == 'V0':
        return progress
    if name == 'V1':
        return 'no progress event since the call started.' + rest
    if name == 'V2':
        return f'no progress event since the call started {elapsed:.1f}s ago.' + rest
    if name == 'V3':
        return rest.strip() or None
    raise ValueError(name)


def frames():
    out = []
    for run in RUNS:
        rows = [json.loads(l) for l in open(LOGS + run + '/trace.jsonl') if l.startswith('{')]
        reqs = {}
        for r in rows:
            if r.get('event') == 'call_supervision_request' and r.get('purpose') == 'running_frame':
                reqs[(r.get('tool_call_id'), r.get('fact_revision'))] = r
            elif r.get('event') == 'call_supervision_result' and r.get('purpose') == 'running_frame':
                q = reqs.get((r.get('tool_call_id'), r.get('fact_revision')))
                if not q or list(q['request']['questions'].get('q', {}).get('criteria', {})) not in (
                        ['keep_going', 'call_agent'], ['call_agent', 'keep_going']):
                    continue
                st = q['request']['state']
                m = re.search(r'elapsed ([0-9.]+)s', st.get('current_work', ''))
                elapsed = float(m.group(1)) if m else -1.0
                ans = ((r.get('response') or {}).get('answers') or {}).get('q', {})
                out.append({'run': run[-14:], 'seq': q['seq'], 'request': q['request'], 'elapsed': elapsed,
                            'no_event': st.get('progress', '').startswith(OLD), 'live': ans.get('choice'),
                            'live_p': (ans.get('probabilities') or {}).get('call_agent')})
    return out


async def ask(client, request):
    for _ in range(3):
        a = await client.ask(request)
        if a.raw:
            q = (a.raw.get('answers') or {}).get('q', {})
            return rq.frame_choice(a.raw, request['questions']), (q.get('probabilities') or {}).get('call_agent')
    return 'unknown', None


async def main():
    all_frames = frames()
    target = [f for f in all_frames if f['no_event']]
    controls = [f for f in all_frames if not f['no_event']][::25]
    print('frames with the line:', len(target), '(early', sum(f['elapsed'] < 3 for f in target), ') controls:', len(controls))
    client = DecisionClient()
    # Baseline: the recorded request as is, recorded option order.
    agree = 0
    for f in target:
        choice, p = await ask(client, json.loads(json.dumps(f['request'])))
        f['replay'] = (choice, p)
        agree += choice == f['live']
    print('baseline replay agrees with live choice: %d/%d' % (agree, len(target)))
    results = collections.defaultdict(lambda: collections.defaultdict(list))
    for name in ('V0', 'V1', 'V2', 'V3'):
        for f in target:
            base = json.loads(json.dumps(f['request']))
            text = variant(name, base['state']['progress'], f['elapsed'])
            state = {}
            for k, v in base['state'].items():
                if k == 'progress':
                    if text is not None:
                        state[k] = text
                else:
                    state[k] = v
            base['state'] = state
            crit = base['questions']['q']['criteria']
            for order in (list(crit), list(reversed(list(crit)))):
                q = json.loads(json.dumps(base))
                q['questions']['q']['criteria'] = {k: crit[k] for k in order}
                choice, p = await ask(client, q)
                bucket = 'early' if f['elapsed'] < 3 else 'late'
                results[name][bucket].append((choice, p, f['run'], f['seq']))
    for name, buckets in results.items():
        for bucket, rows in sorted(buckets.items()):
            ps = [p for _, p, _, _ in rows if p is not None]
            calls = [(r, s) for c, _, r, s in rows if c == 'call_agent']
            print(f'{name} {bucket:5} n={len(rows)} call_agent={len(calls)} median p={statistics.median(ps):.3f} '
                  f'max p={max(ps):.3f}  calls at {sorted(set(calls))[:6]}')
    # Controls (other progress lines) unaffected by construction; report their replay for scale.
    cp = []
    for f in controls:
        _, p = await ask(client, json.loads(json.dumps(f['request'])))
        if p is not None:
            cp.append(p)
    print('controls (progress events present) median p=%.3f max p=%.3f n=%d' % (statistics.median(cp), max(cp), len(cp)))
    late = [f for f in target if f['elapsed'] >= 3]
    for f in late:
        print('late frame', f['run'], f['seq'], 'elapsed', f['elapsed'], f['request']['state'].get('current_work', '')[:90],
              '| live', f['live'], f['live_p'])
    await client.close()

asyncio.run(main())
