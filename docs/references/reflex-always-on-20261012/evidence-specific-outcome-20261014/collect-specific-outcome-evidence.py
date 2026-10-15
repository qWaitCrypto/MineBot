import importlib.util,json,shutil
from pathlib import Path
from collections import Counter
root=Path('/home/qwait/MineBot-always-on');docs=Path('/home/qwait/MineBot/docs/references/reflex-always-on-20261012')
run=root/'logs/reflex-always-on-20261014-specific-outcome-rerun'
assert (run/'launcher-returncode.json').exists()
rows=[json.loads(l) for l in (run/'trace.jsonl').read_text().splitlines()]
spec=importlib.util.spec_from_file_location('metrics',docs/'metrics.py');metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)
summary=metrics.summarize(run)
summary['agent_tool_calls']=dict(Counter(r.get('tool') for r in rows if r['event']=='tool_invoke'))
terminals={}
for line in (run/'client.log').read_text().splitlines():
    marker='MINEBOT_PHASE_A terminal '
    if marker in line:
        data=json.loads(line.split(marker,1)[1])
        if isinstance(data.get('elapsed_ms'),(int,float)) and isinstance(data.get('sampled_at_ms'),(int,float)):terminals[data['action_id']]=data
intervals=metrics.merge([(max(rows[0]['ts'],d['sampled_at_ms']/1000-d['elapsed_ms']/1000),min(rows[-1]['ts'],d['sampled_at_ms']/1000)) for d in terminals.values()])
work=sum(end-start for start,end in intervals)
counts=Counter(r.get('purpose') for r in rows if r['event']=='call_supervision_request' and any(start<=r['ts']<=end for start,end in intervals))
summary['native_client_timing']={'terminal_count':len(terminals),'observed_work_s':work,'remainder_s':summary['elapsed_s']-work,
    'requests_during_work':dict(counts),'requests_per_work_minute':{k:v*60/work for k,v in counts.items()} if work else {},
    'limitation':'Native terminal interval union only; remainder includes Agent inference and settlement, not measured idle.'}
summary['move_terminals']=[{'seq':r['seq'],'arguments':r['arguments'],'success':r['raw_result']['success'],'reason':r['raw_result']['reason'],
    'position':[r['raw_result']['metrics'].get(k) for k in ('final_x','final_y','final_z')]} for r in rows if r['event']=='tool_raw_result' and r.get('tool')=='move_to']
questions=[r for r in rows if r['event']=='call_supervision_request']
summary['interrupt_frames']=[{'seq':r['seq'],'body':r['request']['state'].get('body'),'inventory':r['request']['state'].get('inventory')} for r in questions if 'interrupt' in r['request']['questions'].get('q',{}).get('criteria',{})]
selected=[r for r in rows if r['event'] in {'call_supervision_default_requested','call_supervision_default_finished','direct_call_handoff',
    'reflex_fact_queued','reflex_fact_wake','work_interrupt_handler_started','work_interrupt_handler_finished','work_interrupt_result','reflex_activity_finished','tool_invoke'}
    or r['event']=='call_supervision_result' and r.get('choice') not in {'keep_going','stay'}
    or r['event']=='call_supervision_request'
    or r['event']=='tool_raw_result' and r.get('tool') in {'move_to','equip_item','delegate_work'}]
(run/'metrics.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));(run/'acceptance-events.json').write_text(json.dumps(selected,ensure_ascii=False,indent=2))
(run/'native-terminal-timing.json').write_text(json.dumps(list(terminals.values()),ensure_ascii=False,indent=2))
dest=docs/'evidence-specific-outcome-20261014'
for name in ('acceptance-run.json','fixture-events.jsonl','life-profile.json','launcher-returncode.json','metrics.json','acceptance-events.json','native-terminal-timing.json'):
    shutil.copy2(run/name,dest/name)
for name in ('collect-specific-outcome-evidence.py','start-specific-outcome-rerun.py','control-specific-outcome-rerun.py','specific-outcome-offline-client.sh'):
    shutil.copy2(root/'logs'/name,dest/name)
print(json.dumps({k:summary[k] for k in ('elapsed_s','requests_by_purpose','answers','decision_ms_median','agent_delivery_counts','agent_deliveries','delegate_work','read_reflex','native_client_timing','move_terminals')},ensure_ascii=False,indent=2))
