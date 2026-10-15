"""Collect recorded acceptance evidence; never read provider credentials."""
import importlib.util
import json
from collections import Counter
from pathlib import Path
import uuid

root = Path('/home/qwait/MineBot-always-on')
run = root / 'logs/reflex-always-on-20261014-step7-next'
spec = importlib.util.spec_from_file_location('metrics', '/home/qwait/MineBot/docs/references/reflex-always-on-20261012/metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
summary = metrics.summarize(run)
(run / 'metrics.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
selected = [r for r in rows if 1024 <= r['seq'] <= 1040 or r['event'] in
    ('reflex_fact_wake', 'body_event_wake', 'reflex_fact_dropped', 'reflex_loop_stopped', 'session_ended')]
(run / 'disconnect-events.json').write_text(json.dumps(selected, ensure_ascii=False, indent=2))
server = Path('/tmp/minebot-reflex-options-server')
import hashlib
offline = bytearray(hashlib.md5(b'OfflinePlayer:Step5NextBot').digest())
offline[6] = (offline[6] & 15) | 48
offline[8] = (offline[8] & 63) | 128
player_id = str(uuid.UUID(bytes=bytes(offline)))
import nbtlib
saved = []
for path in server.rglob(player_id + '.dat'):
    nbt = nbtlib.load(path)
    saved.append({'path': str(path), 'health': float(nbt['Health']),
        'position': [float(x) for x in nbt['Pos']],
        'dimension': str(nbt['Dimension']), 'death_time': int(nbt.get('DeathTime', -1))})
(run / 'saved-player-state.json').write_text(json.dumps(saved, indent=2))
for r in rows:
    if r['event'] == 'reflex_death':
        print('death', r['seq'], {k: r.get('facts', {}).get(k) for k in ('health','missing','complete','pos','position','dimension')})
print('saved player state', saved)
print('step7', {k: summary[k] for k in ('elapsed_s','requests_by_purpose','answers','decision_ms_median','agent_delivery_counts','delegate_work','read_reflex','observed_native_work_s','longest_idle_state_without_any_question_s','last_body')})
backup = root / 'logs/reflex-always-on-20261014-step6-backup-next'
data = [json.loads(line) for line in (backup / 'trace.jsonl').read_text().splitlines()]
after = [r for r in data if r['seq'] > 850 and r['event'] == 'call_supervision_result' and r.get('purpose') == 'plan_result_frame']
repeat_summary = {'after_seq':850, 'plan_result_answers':dict(Counter(r.get('choice') for r in after)),
    'result_answer_sequences':[r['seq'] for r in after],
    'equip_choices':[r['seq'] for r in data if r['event']=='call_supervision_result' and str(r.get('choice')).startswith('equip:')]}
(backup / 'repeat-evidence.json').write_text(json.dumps(repeat_summary, indent=2))
print('backup repeats', {k:v for k,v in repeat_summary.items() if k != 'result_answer_sequences'})
