import json, collections, statistics, re
LOGS='/home/qwait/MineBot-always-on/logs/'
RUNS=['reflex-always-on-20261014-step2-done-request','reflex-always-on-20261014-step6-spare-claude3',
      'reflex-always-on-20261014-step5-threat-claude7','reflex-always-on-20261014-step5-threat-claude8',
      'reflex-always-on-20261014-step5-threat-claude9','reflex-always-on-20261014-step5-threat-claude10']
buckets=collections.defaultdict(list)
samples=[]
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
            if sorted(crit)!=['call_agent','keep_going']: continue
            st=q['request']['state']
            ans=((r.get('response') or {}).get('answers') or {}).get('q',{})
            p=(ans.get('probabilities') or {}).get('call_agent')
            if p is None: continue
            m=re.search(r'elapsed ([0-9.]+)s', st.get('current_work',''))
            e=float(m.group(1)) if m else -1
            prog=st.get('progress','')
            pk=('none' if not prog else 'no-real-progress' if prog.startswith('no real progress') else 'last-event')
            moved=bool(re.search(r'started at (\[[^\]]+\]); now at \1', st.get('position','')))
            buckets[('early' if e<3 else 'late', pk, 'same-pos' if moved else 'moved')].append(p)
            if e<3 and len(samples)<3 and p>0.5: samples.append((run[-12:], q['seq'], st))
for k,v in sorted(buckets.items()):
    print(k, 'n',len(v), 'median %.3f' % statistics.median(v), 'max %.3f'%max(v), '>0.5:',sum(x>0.5 for x in v))
for s in samples: print(s[0], s[1], json.dumps(s[2], ensure_ascii=False)[:900])
