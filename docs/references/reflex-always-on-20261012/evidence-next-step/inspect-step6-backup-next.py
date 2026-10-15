import json
from pathlib import Path
run = Path('/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step6-backup-next')
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
for r in rows:
    if r['event'] in {'work_call', 'work_call_result', 'call_supervision_default_requested',
        'call_supervision_default_finished', 'reflex_activity_finished', 'reflex_fact_wake',
        'reflex_fact_queued', 'agent_error', 'tool_invoke', 'tool_result'}:
        if r['event'] == 'tool_result':
            print(r['seq'], r['event'], r.get('tool'), json.dumps(r.get('full_result'), ensure_ascii=False)[:1300])
        elif r['event'] == 'reflex_activity_finished':
            print(r['seq'], r['event'], r.get('status'))
        else:
            print(r['seq'], r['event'], json.dumps(r, ensure_ascii=False)[:1800])
    if r['event'] == 'call_supervision_result' and r.get('purpose') in {'plan_result_frame', 'default_result_frame'}:
        print(r['seq'], r['event'], r.get('choice'))
print('equip choices', [r['seq'] for r in rows if r['event'] == 'call_supervision_result' and str(r.get('choice')).startswith('equip:')])
