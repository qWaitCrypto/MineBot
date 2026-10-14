import json
from pathlib import Path
run=Path('/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-idle-equipment-context-fixed')
rows=[json.loads(line) for line in (run/'trace.jsonl').read_text().splitlines()]
questions=[r for r in rows if r['seq']>454 and r['event']=='call_supervision_request' and r.get('purpose')=='default_result_frame']
for r in questions:
    facts=r['compiler_input']['decision_context']['body_state']
    entities=facts.get('entities',facts.get('nearby_entities',[]))
    print(r['seq'],json.dumps(r['request'],ensure_ascii=False))
    print('hostile raw',json.dumps([e for e in entities if e.get('hostile')],ensure_ascii=False))
out=run/'post-kill-requests.json'
out.write_text(json.dumps(questions,ensure_ascii=False,indent=2))
