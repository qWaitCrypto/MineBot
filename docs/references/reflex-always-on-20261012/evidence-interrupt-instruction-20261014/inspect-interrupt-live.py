import json
from pathlib import Path
run = Path('/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-interrupt-instruction-fixed')
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
for row in rows:
    if row['event'] == 'call_supervision_request' and row.get('purpose') == 'running_frame':
        print(json.dumps({'seq': row['seq'], 'call_id': row.get('tool_call_id'),
                         'inventory': row['request']['state'].get('inventory'),
                         'body': row['request']['state'].get('body'),
                         'options': list(row['request']['questions']['q']['criteria'])}, ensure_ascii=False))
    elif row['event'] == 'call_supervision_result':
        print(json.dumps({'seq': row['seq'], 'purpose': row.get('purpose'), 'tool': row.get('tool'),
                         'choice': row.get('choice')}, ensure_ascii=False))
    elif row['event'].startswith('work_interrupt') or row['event'] == 'reflex_activity_finished':
        print(json.dumps({key: row[key] for key in ('seq', 'event', 'status', 'node_id', 'attempt') if key in row}))
