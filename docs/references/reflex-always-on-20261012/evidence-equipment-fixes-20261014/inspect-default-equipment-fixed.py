import json
import sys
from collections import Counter
from pathlib import Path
run=Path(sys.argv[1]) if len(sys.argv)>1 else Path('/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-default-equipment-fixed')
rows=[]
for line in (run/'trace.jsonl').read_text().splitlines():
    try:rows.append(json.loads(line))
    except ValueError:pass
for r in rows:
    event=r['event']
    if event=='call_supervision_result' and r.get('choice') not in {'stay','keep_going'}:
        print(r['seq'],event,r.get('purpose'),r.get('choice'))
    if event=='tool_raw_result' and r.get('tool') in {'equip_item','engage_entity','move_to'}:
        payload=r.get('raw_result') or {}
        print(r['seq'],event,r.get('tool'),payload.get('success'),payload.get('reason'),{k:(payload.get('metrics') or {}).get(k) for k in ('broken_item','selected_item','target_health','attacks')})
    if event.startswith('reflex_fact_') or event in {'body_event_wake','reflex_activity_finished','work_equipment_started','work_equipment_finished'}:
        print(r['seq'],event,r.get('status'),r.get('reason'))
print('last',rows[-1]['seq'],rows[-1]['event'])
print('requests',dict(Counter(r.get('purpose') for r in rows if r['event']=='call_supervision_request')))
print('tools',dict(Counter(r.get('tool') for r in rows if r['event']=='tool_invoke')))
