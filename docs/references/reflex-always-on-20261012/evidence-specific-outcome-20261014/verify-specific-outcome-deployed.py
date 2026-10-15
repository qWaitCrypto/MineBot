"""Verify the deployed compiler emits each offline-tested candidate verbatim."""
from pathlib import Path
root=Path('/home/qwait/MineBot-always-on')
exec(compile((root/'logs/expanded-report-observation-trial.py').read_text().split('\nasync def main():')[0],__file__,'exec'))
expected={}
for line in (root/'logs/expanded-report-observation-trial-b3ac247.jsonl').read_text().splitlines():
    row=json.loads(line)
    if row['variant']=='candidate':expected.setdefault(row['case'],row['request'])
actual={}
for name,seq,running in [('player-handler-fixed',333,True),('player-handler-fixed',862,True),('life-30-final-v2',4863,False)]:
    rows=[json.loads(line) for line in (root/f'logs/reflex-always-on-20261014-{name}/trace.jsonl').read_text().splitlines()]
    row=next(r for r in rows if r['seq']==seq)
    row['request']=expected[f'{name}:{seq}']
    actual[f'{name}:{seq}']=compile_online(row) if running else compile_terminal(row)
for case in json.loads((root/'tests/fixtures/reflex_step2_running_intent_frames.json').read_text())['cases']:
    actual[f'running:{case["seq"]}']=compile_fixture(case)
for case in json.loads((root/'tests/fixtures/reflex_step2_plan_result_frames.json').read_text())['cases']:
    actual[f'plan_result:{case["seq"]}']=compile_plan(case)
for name in ('broken-tool-frame-v3','unrelated-frame'):
    frame=json.loads((root/f'logs/reflex-always-on-20261014c-step6-native/{name}.json').read_text())
    node={'kind':'call','id':'fight','tool':frame['tool'],'arguments':frame['arguments'],'intent':frame['intent']}
    case={'node':node,'node_id':'fight','intents':[frame['intent']], 'plan':{'language':'minebot-work/0.2','goal':frame['intent'],'intent':frame['intent'],'timeout_s':30,'work':node},
        'raw_decision_facts':{'tool_result':_model_tool_payload(frame['tool'],frame['result'],trace_ref=name),'tool_arguments':frame['arguments'],'body_state':frame['facts']}}
    actual[name]=compile_plan(case)
assert set(actual)==set(expected)
for name,request in actual.items():
    assert json.dumps(request,ensure_ascii=False,separators=(',',':'))==json.dumps(expected[name],ensure_ascii=False,separators=(',',':')),name
print(json.dumps({'deployed_candidate_exact':len(actual),'commit':'c0ed3ee'}))
