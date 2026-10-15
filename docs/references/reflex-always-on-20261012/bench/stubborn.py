import json
LOGS='/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-'
for run, seq in (('step6-spare-claude3', 10541), ('step5-threat-claude7', 1885), ('step5-threat-claude7', 3891), ('step5-threat-claude8', 1810)):
    for l in open(LOGS+run+'/trace.jsonl'):
        if f'"seq": {seq},' in l:
            r=json.loads(l); q=r['request']
            print('=====', run, seq)
            for k,v in q['state'].items(): print('  ', k, ':', v)
            for k,v in q['questions']['q']['criteria'].items(): print('  OPT', k, ':', v)
            break
