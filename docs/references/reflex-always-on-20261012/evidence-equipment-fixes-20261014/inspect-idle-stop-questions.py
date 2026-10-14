import json
from pathlib import Path
rows=[json.loads(line) for line in Path('logs/reflex-always-on-20261014-idle-equipment-fixed/trace.jsonl').read_text().splitlines()]
for answer in rows:
    if answer['event']=='call_supervision_result' and answer.get('purpose')=='running_frame' and answer.get('choice')=='call_agent':
        question=next(r for r in reversed(rows) if r['event']=='call_supervision_request' and r['seq']<answer['seq']
            and r.get('tool_call_id')==answer.get('tool_call_id') and r.get('fact_revision')==answer.get('fact_revision'))
        print(answer['seq'],json.dumps(question['request'],ensure_ascii=False))
