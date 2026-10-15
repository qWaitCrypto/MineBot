"""Trace metrics plus native terminal timing omitted by some public receipts."""
import importlib.util
import json
from collections import Counter
from pathlib import Path
import shutil

root=Path('/home/qwait/MineBot-always-on')
main=Path('/home/qwait/MineBot/docs/references/reflex-always-on-20261012')
spec=importlib.util.spec_from_file_location('metrics',main/'metrics.py')
metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)
dest=main/'evidence-equipment-fixes-20261014';dest.mkdir(exist_ok=True)
reports=[]
for suffix in ('default-equipment-fixed','idle-equipment-fixed','idle-equipment-context-fixed','idle-equipment-dead-fixed'):
    run=root/('logs/reflex-always-on-20261014-'+suffix)
    assert json.loads((run/'launcher-returncode.json').read_text())['exit_code']==0
    rows=[json.loads(line) for line in (run/'trace.jsonl').read_text().splitlines()]
    summary=metrics.summarize(run)
    terminals={}
    for line in (run/'client.log').read_text().splitlines():
        marker='MINEBOT_PHASE_A terminal '
        if marker not in line:continue
        payload=json.loads(line.split(marker,1)[1])
        if isinstance(payload.get('elapsed_ms'),(int,float)) and isinstance(payload.get('sampled_at_ms'),(int,float)):
            terminals[payload['action_id']]={key:payload.get(key) for key in ('action_id','action_kind','reason','elapsed_ms','sampled_at_ms')}
    intervals=metrics.merge([(max(rows[0]['ts'],r['sampled_at_ms']/1000-r['elapsed_ms']/1000),min(rows[-1]['ts'],r['sampled_at_ms']/1000)) for r in terminals.values()])
    seconds=sum(end-start for start,end in intervals)
    counts=Counter(r.get('purpose') for r in rows if r['event']=='call_supervision_request' and any(start<=r['ts']<=end for start,end in intervals))
    summary['native_client_timing']={'source':'client.log MINEBOT_PHASE_A terminal JSON, deduplicated by action_id',
        'terminal_count':len(terminals),'observed_work_s':seconds,'remainder_s':summary['elapsed_s']-seconds,
        'requests_during_work':dict(counts),'requests_per_work_minute':{key:value*60/seconds for key,value in counts.items()} if seconds else None,
        'limitation':'Only terminals with native timing are measured. Remainder includes startup and settlement; it is not proof of physical idleness.'}
    (run/'metrics.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    (run/'native-terminal-timing.json').write_text(json.dumps(list(terminals.values()),indent=2))
    selected=[r for r in rows if (r['event']=='call_supervision_result' and (r.get('choice') not in {'keep_going','stay','unknown'} or r.get('purpose')=='default_result_frame'))
        or (r['event']=='call_supervision_request' and r.get('purpose')=='default_result_frame')
        or r['event'].startswith('reflex_fact_') or r['event'] in {'tool_raw_result','reflex_activity_finished'}]
    (run/'acceptance-events.json').write_text(json.dumps(selected,ensure_ascii=False,indent=2))
    out=dest/suffix;out.mkdir(exist_ok=True)
    for name in ('acceptance-run.json','fixture-events.jsonl','metrics.json','native-terminal-timing.json','acceptance-events.json','launcher-returncode.json'):
        shutil.copy2(run/name,out/name)
    if (run/'post-kill-requests.json').exists():shutil.copy2(run/'post-kill-requests.json',out/'post-kill-requests.json')
    reports.append({'run':suffix,**{k:summary[k] for k in ('elapsed_s','requests_by_purpose','answers','decision_ms_median','agent_delivery_counts','agent_deliveries','delegate_work','read_reflex','native_client_timing')}})
    print(json.dumps(reports[-1],ensure_ascii=False))
for name in ('missing-native-state-before.log','missing-native-state-after.log','default-equipment-before.log','default-equipment-after.log',
    'idle-protection-result-before.log','idle-protection-result-after.log','idle-task-context-before.log','idle-task-context-after.log',
    'dead-hostile-before.log','dead-hostile-after.log','dead-hostile-trial.py','dead-hostile-trial-06bca71.jsonl','dead-hostile-trial-06bca71.summary.json',
    'default-equipment-trial-17af08d.jsonl','default-equipment-trial-17af08d.summary.json',
    'default-equipment-online-17af08d.jsonl','default-equipment-online-17af08d.summary.json',
    'idle-equipment-trial-6053ca4.jsonl','idle-equipment-trial-6053ca4.summary.json',
    'idle-task-context-trial-58145e2.jsonl','idle-task-context-trial-58145e2.summary.json',
    'default-equipment-trial.py','idle-equipment-trial.py','idle-task-context-trial.py',
    'start-default-equipment-fixed.py','control-default-equipment-fixed.py',
    'start-idle-equipment-fixed.py','control-idle-equipment-fixed.py',
    'start-idle-equipment-context-fixed.py','control-idle-equipment-context-fixed.py',
    'start-idle-equipment-dead-fixed.py','control-idle-equipment-dead-fixed.py',
    'inspect-post-kill-questions.py',
    'inspect-default-equipment-fixed.py','inspect-idle-stop-questions.py','collect-equipment-fixes-evidence.py'):
    shutil.copy2(root/'logs'/name,dest/name)
(dest/'metrics-all.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))
