"""Offline raw-frame binding regression. No Decision or Minecraft calls."""
import copy
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

root = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(root))
from minebot.app.body_loop import BodyLoop
from minebot.app import direct_call_supervision as supervision

run = root / 'logs/reflex-always-on-20261014-step7-current-f'
profile = json.loads((run / 'life-profile.json').read_text())
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
requests = {}
bindings = []
for row in rows:
    if row['event'] == 'call_supervision_request':
        requests[(row['tool_call_id'], row.get('fact_revision'))] = row
    if row['event'] != 'call_supervision_result' or row.get('purpose') != 'running_frame' or row.get('choice') != 'go_home':
        continue
    request = requests[(row['tool_call_id'], row.get('fact_revision'))]
    inp = request['frame_input']
    raw = copy.deepcopy(inp['execution'])
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace(),
        trace=SimpleNamespace(emit=lambda *args, **kwargs: None)))
    loop._life_profile = copy.deepcopy(profile)
    loop._ambient_facts = copy.deepcopy(inp.get('ambient_facts', {}))
    loop._vitals._samples = copy.deepcopy(inp.get('vital_samples', []))
    loop.safe_standing_position = copy.deepcopy(inp.get('safe_standing_position'))
    with patch.object(supervision.time, 'monotonic', return_value=inp['monotonic_s']):
        full = loop._frame(raw)
    compiled = request['compiler_input']
    context = compiled['supervision_context']
    params = compiled['params']
    tool = SimpleNamespace(name=request['tool'])
    record = {'tool': request['tool'], 'params': params, 'supervision_context': context}
    options = loop._options_for(tool, record, execution=full)
    generated = loop._question(request['tool_call_id'], tool, params, full,
        idle_s=request.get('idle_s', 0), purpose='running_frame',
        supervision_context=context, options=options)
    generated.pop('_request_chars_before', None)
    generated.pop('_request_chars', None)
    bindings.append({'request_seq': request['seq'], 'answer_seq': row['seq'],
        'tool_call_id': row['tool_call_id'], 'fact_revision': row.get('fact_revision'),
        'question_unchanged': generated == request['request'],
        'old_raw_binding': loop._default_action('go_home', raw),
        'asked_frame_binding': loop._default_action('go_home', full)})
summary = {'source': str(run / 'trace.jsonl'), 'frame_count': len(bindings),
           'questions_equal': sum(r['question_unchanged'] for r in bindings),
           'old_raw_binding_available': sum(r['old_raw_binding'] is not None for r in bindings),
           'asked_frame_binding_available': sum(r['asked_frame_binding'] is not None for r in bindings),
           'scope': 'Recorded choices, raw frame reconstruction and deterministic binding; no new model judgement and no physical execution.',
           'frames': bindings}
(root / 'logs/answer-bindings-replay.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps({k: v for k, v in summary.items() if k != 'frames'}, ensure_ascii=False))
assert len(bindings) == 109
assert all(r['question_unchanged'] for r in bindings)
assert all(r['old_raw_binding'] is None for r in bindings)
assert all(r['asked_frame_binding'] is not None for r in bindings)
