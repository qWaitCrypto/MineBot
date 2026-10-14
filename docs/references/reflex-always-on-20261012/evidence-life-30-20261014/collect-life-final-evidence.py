"""Preserve terminal life evidence; no assertions that missing data is success."""
import importlib.util
import json
from collections import Counter
from pathlib import Path
import shutil

root = Path('/home/qwait/MineBot-always-on')
docs = Path('/home/qwait/MineBot/docs/references/reflex-always-on-20261012')
run = root / 'logs/reflex-always-on-20261014-life-30-final-v2'
assert (run / 'launcher-returncode.json').exists(), 'Wait for the actual owner terminal'
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
spec = importlib.util.spec_from_file_location('metrics', docs / 'metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
summary = metrics.summarize(run)
terminals = {}
for line in (run / 'client.log').read_text().splitlines():
    marker = 'MINEBOT_PHASE_A terminal '
    if marker not in line:
        continue
    data = json.loads(line.split(marker, 1)[1])
    if isinstance(data.get('elapsed_ms'), (int, float)) and isinstance(data.get('sampled_at_ms'), (int, float)):
        terminals[data['action_id']] = data
intervals = metrics.merge([(max(rows[0]['ts'], data['sampled_at_ms'] / 1000 - data['elapsed_ms'] / 1000),
    min(rows[-1]['ts'], data['sampled_at_ms'] / 1000)) for data in terminals.values()])
seconds = sum(end - start for start, end in intervals)
counts = Counter(r.get('purpose') for r in rows if r['event'] == 'call_supervision_request'
    and any(start <= r['ts'] <= end for start, end in intervals))
summary['native_client_timing'] = {'terminal_count': len(terminals), 'observed_work_s': seconds,
    'remainder_s': summary['elapsed_s'] - seconds, 'requests_during_work': dict(counts),
    'requests_per_work_minute': {key: value * 60 / seconds for key, value in counts.items()} if seconds else None,
    'limitation': 'Union of terminal intervals; remainder includes startup, settlement and missing terminals.'}
summary['agent_tool_calls'] = dict(Counter(r.get('tool') for r in rows if r['event'] == 'tool_invoke'))
summary['distinct_decision_actions'] = sorted(set(r.get('choice') for r in rows
    if r['event'] == 'call_supervision_default_requested'))
summary['native_world_edit_events'] = [r for r in rows if r['event'] in {'world_edit', 'body_world_edit'}]
summary['completion_validation'] = [{key: r.get(key) for key in ('seq', 'ts', 'tool', 'raw_result')}
    for r in rows if r['event'] == 'tool_raw_result' and r.get('tool') == 'checkpoint_task']
summary['claims_excluded'] = ['Not a fresh world or empty inventory.',
    'Visitor fell from the platform; no claim that the bot killed it.',
    'goal_replaced cancellation is not life-action tool-admission preemption evidence.',
    'No claim of task completion based on move_to success.',
    'No world-edit events alone do not prove player-block preservation.']
(run / 'metrics.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
(run / 'native-terminal-timing.json').write_text(json.dumps(list(terminals.values()), ensure_ascii=False, indent=2))
events = [r for r in rows if r['event'] in {'reflex_loop_started', 'reflex_loop_stopped', 'reflex_state_changed',
    'call_supervision_default_requested', 'call_supervision_default_finished', 'execution_cancel_requested',
    'execution_cancelled', 'reflex_fact_wake', 'body_event_wake', 'runtime_intent_wake',
    'reflex_activity_finished', 'task_completed', 'tool_invoke'}
    or r['event'] == 'tool_raw_result' and r.get('tool') in {'move_to', 'checkpoint_task', 'read_reflex'}]
(run / 'acceptance-events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2))
dest = docs / 'evidence-life-30-20261014'
dest.mkdir(exist_ok=True)
for name in ('acceptance-run.json', 'fixture-events.jsonl', 'life-profile.json', 'launcher-returncode.json',
             'metrics.json', 'native-terminal-timing.json', 'acceptance-events.json'):
    shutil.copy2(run / name, dest / name)
for name in ('collect-life-final-evidence.py', 'start-life-30-final.py', 'control-life-30-final.py',
    'start-life-fixed-g.py', 'control-step7-next.py', 'inspect-life-player-facts.py',
    'life-player-alias-trial.py', 'life-player-alias-trial-0a2840b.jsonl',
    'life-player-alias-trial-0a2840b.summary.json', 'r3-negative-current-replay.py',
    'r3-negative-current-replay-0a2840b.jsonl', 'r3-negative-current-replay-0a2840b.summary.json',
    'handler-continuation-trial.py', 'handler-continuation-trial-0a2840b.jsonl',
    'handler-continuation-trial-0a2840b.summary.json'):
    shutil.copy2(root / 'logs' / name, dest / name)
suite = dest / 'full-suite-0a2840b'
suite.mkdir(exist_ok=True)
for name in ('pytest.log', 'running.json', 'exit.json'):
    shutil.copy2(root / 'logs/final-suite-0a2840b' / name, suite / name)
print(json.dumps({k: summary[k] for k in ('elapsed_s', 'requests_by_purpose', 'answers', 'decision_ms_median',
    'agent_delivery_counts', 'agent_deliveries', 'read_reflex', 'delegate_work', 'last_body', 'native_client_timing')},
    ensure_ascii=False, indent=2))
