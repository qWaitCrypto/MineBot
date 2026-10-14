import json
from pathlib import Path
run = Path('/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-life-30-final-v2')
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
matches = []
for row in rows:
    if row['event'] != 'call_supervision_request':
        continue
    facts = row.get('compiler_input', {}).get('facts', {})
    players = {}
    for key in ('entities', 'nearby_entities', 'entities_near'):
        seen = [entity for entity in facts.get(key, []) if entity.get('type')=='player' or entity.get('name')=='Visitor']
        if seen:
            players[key] = [{key:e.get(key) for key in ('id','type','name','dist','dist2','pos')} for e in seen]
    if players:
        matches.append({'seq':row['seq'],'players_in_compiler_facts':players,'request':row['request']})
print('frames containing player facts:', len(matches))
for item in matches[:2] + matches[-1:]:
    print(json.dumps(item,ensure_ascii=False))
(run / 'player-facts-inspection.json').write_text(json.dumps(matches,ensure_ascii=False,indent=2))
