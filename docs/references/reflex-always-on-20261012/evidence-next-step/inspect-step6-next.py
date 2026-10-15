import json
from pathlib import Path
run = Path('/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step6-next')
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
for row in rows:
    if row['event'] == 'tool_result' and row.get('tool') == 'delegate_work':
        print('accepted', row['seq'], json.dumps(row.get('full_result', {}).get('metrics', {}).get('tree'), ensure_ascii=False))
    if row['event'] in {'reflex_activity_finished', 'reflex_fact_queued', 'reflex_fact_wake', 'reflex_fact_delivery_deferred'}:
        print('event', row['seq'], json.dumps(row, ensure_ascii=False)[:2200])
frames = [r for r in rows if r['event'] == 'call_supervision_request' and r['seq'] > 303]
for r in frames[:4]:
    print('question', r['seq'], json.dumps(r.get('question', r.get('request', {})), ensure_ascii=False))
print('interrupt-bearing frames', sum('interrupt' in r.get('question', r.get('request', {})).get('questions', {}).get('q', {}).get('criteria', {}) for r in frames))
