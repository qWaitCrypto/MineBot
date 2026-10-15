import json, collections, sys
R=(sys.argv[1] if len(sys.argv)>1 else '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step8-extra-claude1')+'/'
rows=[json.loads(l) for l in open(R+'trace.jsonl') if l.startswith('{')]
fx=[json.loads(l) for l in open(R+'fixture-events.jsonl')]
scenes=[(f['label'], f.get('attempt'), f['ts']) for f in fx if f['kind']=='scene']
ends=[f['ts'] for f in fx if f['kind']=='scene_end']+[rows[-1]['ts']]
for i,(label, attempt, a) in enumerate(scenes):
    b=min([e for e in ends if e>a]+[rows[-1]['ts']])
    acts=[(round(r['ts']-a,1), r.get('choice'), r.get('tool')) for r in rows if a<=r['ts']<=b and r.get('event')=='call_supervision_default_requested']
    ans=collections.Counter((r.get('purpose'), r.get('choice')) for r in rows if a<=r['ts']<=b and r.get('event')=='call_supervision_result')
    offered=collections.Counter()
    for r in rows:
        if a<=r['ts']<=b and r.get('event')=='call_supervision_request':
            for k in r['request'].get('questions',{}).get('q',{}).get('criteria',{}): offered[k]+=1
            for k in r['request'].get('questions',{}).get('p',{}).get('criteria',{}): offered['p:'+k]+=1
    hp=[r.get('health') for r in rows if a<=r['ts']<=b and r.get('event')=='body_progress' and r.get('health') is not None]
    print(f'=== {label} {attempt if attempt is not None else ""} ({round(b-a)} s) hp {hp[:1]}->{hp[-1:]} min {min(hp) if hp else None}')
    print('   actions:', acts[:12])
    print('   answers:', dict(ans.most_common(10)))
    print('   offered:', dict(offered.most_common(14)))
    res=[(r['seq'], r.get('tool'), (r.get('raw_result') or {}).get('reason')) for r in rows if a<=r['ts']<=b and r.get('event')=='tool_raw_result' and r.get('tool') in ('engage_entity','ranged_attack','follow_entity','search_for_entity','equip_item','move_away')]
    print('   results:', res[:10])
print('deaths', sum(r.get('event')=='reflex_death' for r in rows))
