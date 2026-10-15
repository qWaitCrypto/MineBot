import json, collections, statistics
LOGS='/home/qwait/MineBot-always-on/logs/'
RUNS=['reflex-always-on-20261014-step2-done-request','reflex-always-on-20261014-step6-spare-claude3',
      'reflex-always-on-20261014-step5-threat-claude7','reflex-always-on-20261014-step5-threat-claude8',
      'reflex-always-on-20261014-step5-threat-claude9','reflex-always-on-20261014-step5-threat-claude10']
groups=collections.defaultdict(lambda: collections.Counter()); probs=collections.defaultdict(list); early=collections.Counter()
for run in RUNS:
    rows=[json.loads(l) for l in open(LOGS+run+'/trace.jsonl') if l.startswith('{')]
    reqs={}
    for r in rows:
        if r.get('event')=='call_supervision_request' and r.get('purpose')=='running_frame':
            reqs[(r.get('tool_call_id'), r.get('fact_revision'))]=r
        elif r.get('event')=='call_supervision_result' and r.get('purpose')=='running_frame':
            q=reqs.get((r.get('tool_call_id'), r.get('fact_revision')))
            if not q: continue
            crit=q['request']['questions'].get('q',{}).get('criteria',{})
            key=tuple(sorted(crit))
            if 'protect' in str(q['request']['questions'].get('q',{}).get('instructions','')).lower(): key=('PROTECTING',)+key
            ans=((r.get('response') or {}).get('answers') or {}).get('q',{})
            groups[key][ans.get('choice') or r.get('choice')]+=1
            p=(ans.get('probabilities') or {}).get('call_agent')
            if p is not None: probs[key].append(p)
            el=(q['request']['state'].get('current_work') or '')
            if key==('call_agent','keep_going'):
                import re
                m=re.search(r'elapsed ([0-9.]+)s', el)
                e=float(m.group(1)) if m else -1
                early[('<3s' if 0<=e<3 else '>=3s', ans.get('choice'))]+=1
for key,c in sorted(groups.items(), key=lambda kv:-sum(kv[1].values())):
    ps=probs[key]
    print(key, dict(c), 'n',sum(c.values()), 'p(call_agent) median %.3f p90 %.3f' % (statistics.median(ps), sorted(ps)[int(len(ps)*.9)]) if ps else '')
print('two-option frames by elapsed:', dict(early))
