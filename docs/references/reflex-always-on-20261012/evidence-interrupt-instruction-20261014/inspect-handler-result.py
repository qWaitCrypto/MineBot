import json
from pathlib import Path
run = Path('/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-interrupt-instruction-fixed')
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
request = next(r for r in rows if r['event'] == 'call_supervision_request'
               and r.get('purpose') == 'plan_result_frame' and r.get('tool') == 'equip_item')
print(json.dumps({'seq': request['seq'], 'request': request['request'],
                  'supervision_context': request['compiler_input']['supervision_context']}, ensure_ascii=False, indent=2))
