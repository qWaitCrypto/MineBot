"""Collect the narrow physical successes and the premature terminal failure."""
import importlib.util
import json
from collections import Counter
from pathlib import Path
import shutil

root = Path('/home/qwait/MineBot-always-on')
docs = Path('/home/qwait/MineBot/docs/references/reflex-always-on-20261012')
run = root / 'logs/reflex-always-on-20261014-player-handler-fixed'
assert (run / 'launcher-returncode.json').exists()
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
spec = importlib.util.spec_from_file_location('metrics', docs / 'metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
summary = metrics.summarize(run)
summary['agent_tool_calls'] = dict(Counter(r.get('tool') for r in rows if r['event'] == 'tool_invoke'))
summary['distinct_decision_actions'] = sorted(set(r.get('choice') for r in rows
    if r['event'] == 'call_supervision_default_requested') | {'interrupt', 'resume'})
summary['failure'] = {'premature_stop_request_seq': 862, 'expected': 'keep_going',
    'target_position': [220, 121, 220], 'native_final_position': [206.74008212499456, 121, 220.50075239384566],
    'runtime_terminal_status': 'completed', 'task_physically_completed': False}
terminals = {}
for line in (run / 'client.log').read_text().splitlines():
    marker = 'MINEBOT_PHASE_A terminal '
    if marker not in line:
        continue
    data = json.loads(line.split(marker, 1)[1])
    if isinstance(data.get('elapsed_ms'), (int, float)) and isinstance(data.get('sampled_at_ms'), (int, float)):
        terminals[data['action_id']] = data
intervals = metrics.merge([(max(rows[0]['ts'], d['sampled_at_ms'] / 1000 - d['elapsed_ms'] / 1000),
    min(rows[-1]['ts'], d['sampled_at_ms'] / 1000)) for d in terminals.values()])
seconds = sum(end - start for start, end in intervals)
counts = Counter(r.get('purpose') for r in rows if r['event'] == 'call_supervision_request'
    and any(start <= r['ts'] <= end for start, end in intervals))
summary['native_client_timing'] = {'terminal_count': len(terminals), 'observed_work_s': seconds,
    'remainder_s': summary['elapsed_s'] - seconds, 'requests_during_work': dict(counts),
    'requests_per_work_minute': {key: value * 60 / seconds for key, value in counts.items()} if seconds else None,
    'limitation': 'Union of terminal intervals; remainder includes startup, settlement and missing terminals.'}
selected = [r for r in rows if r['event'] in {'call_supervision_default_requested', 'call_supervision_default_finished',
    'direct_call_handoff', 'reflex_fact_queued', 'reflex_fact_wake', 'work_interrupt_handler_started',
    'work_interrupt_handler_finished', 'work_interrupt_result', 'reflex_activity_finished', 'tool_invoke'}
    or r['event'] == 'call_supervision_result' and r.get('choice') not in {'keep_going', 'stay', 'unknown'}
    or r['event'] == 'call_supervision_request' and (r.get('purpose') == 'plan_result_frame' or r['seq'] == 862)
    or r['event'] == 'tool_raw_result' and r.get('tool') in {'move_to', 'equip_item', 'delegate_work'}]
(run / 'metrics.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
(run / 'acceptance-events.json').write_text(json.dumps(selected, ensure_ascii=False, indent=2))
(run / 'native-terminal-timing.json').write_text(json.dumps(list(terminals.values()), ensure_ascii=False, indent=2))
dest = docs / 'evidence-player-handler-20261014'
dest.mkdir(exist_ok=True)
for name in ('acceptance-run.json', 'fixture-events.jsonl', 'life-profile.json', 'launcher-returncode.json',
             'metrics.json', 'native-terminal-timing.json', 'acceptance-events.json'):
    shutil.copy2(run / name, dest / name)
for name in ('collect-player-handler-evidence.py', 'start-player-handler-fixed.py', 'control-player-handler-fixed.py',
    'player-handler-focused.log', 'pending-facts-handler-before.log', 'pending-facts-handler-after.log',
    'test_pending_fact_and_handler_regressions.py', 'handler-resume-frame-trial.py',
    'handler-resume-frame-trial-0a2840b.jsonl', 'handler-resume-frame-trial-0a2840b.summary.json',
    'patrol-early-stop-replay.py', 'patrol-early-stop-replay-5703f3f.jsonl',
    'patrol-early-stop-replay-5703f3f.summary.json', 'r3-target-reason-trial.py',
    'r3-target-reason-trial-5703f3f.jsonl', 'r3-target-reason-trial-5703f3f.summary.json'):
    shutil.copy2(root / 'logs' / name, dest / name)
print(json.dumps({k: summary[k] for k in ('elapsed_s', 'requests_by_purpose', 'answers', 'decision_ms_median',
    'agent_delivery_counts', 'agent_deliveries', 'delegate_work', 'read_reflex', 'last_body', 'native_client_timing')},
    ensure_ascii=False, indent=2))
