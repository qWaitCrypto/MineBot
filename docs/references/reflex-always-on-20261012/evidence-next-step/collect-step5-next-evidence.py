import importlib.util
import json
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
spec = importlib.util.spec_from_file_location('metrics', '/home/qwait/MineBot/docs/references/reflex-always-on-20261012/metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
for suffix in ('', '-b', '-c', '-d'):
    run = root / ('logs/reflex-always-on-20261014-step5-next' + suffix)
    rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
    measured = metrics.summarize(run)
    (run / 'metrics.json').write_text(json.dumps(measured, ensure_ascii=False, indent=2))
    interesting = {'call_supervision_default_requested', 'call_supervision_default_finished',
        'reflex_reaction_chain', 'reflex_fact_wake', 'reflex_activity_finished', 'reflex_activity_blocked',
        'reflex_death', 'call_supervision_finished', 'body_execution_terminal'}
    selected = [r for r in rows if r['event'] in interesting]
    (run / 'acceptance-events.json').write_text(json.dumps(selected, ensure_ascii=False, indent=2))
    print('RUN', run.name)
    for key in ('duration_s', 'requests_by_purpose', 'request_counts_by_purpose', 'answers',
                'decision_ms_median', 'agent_delivery_counts', 'delegate_work', 'read_reflex',
                'supervised_body_call_s', 'native_body_work_s', 'body_work_s', 'deaths'):
        if key in measured:
            print(key, json.dumps(measured[key], ensure_ascii=False))
    for r in selected:
        if r['event'] == 'call_supervision_finished':
            continue
        print(r['seq'], r['event'], json.dumps({k: r[k] for k in
            ('status', 'choice', 'tool', 'tool_call_id', 'result', 'reason', 'total_s', 'event_health',
             'event_distance', 'event_to_request_s', 'request_to_answer_s', 'answer_to_action_s') if k in r}, ensure_ascii=False)[:1500])
    print('native terminals')
    for r in rows:
        if r['event'] == 'body_event' and isinstance(r.get('data'), dict):
            print(r['seq'], str(r)[:600])
    print('last full', json.dumps(measured.get('last_body'), ensure_ascii=False))
