"""Equal-input compiler baseline; missing historical snapshots are NOT reconstructed."""
import importlib.util
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from minebot.app.direct_call_supervision import CallSupervisor


def test_step2_new_old_protocol_on_118_inputs_except_intent_wording_and_main_hand():
    root = Path(__file__).parents[2]
    fixture = json.loads((root / 'tests/fixtures/reflex_step2_equivalent_inputs.json').read_text())
    cases = fixture['cases']
    assert len(cases) == 118
    assert sum(bool(c['execution']['facts']) for c in cases) == 109
    with tempfile.TemporaryDirectory(prefix='minebot-step2-baseline-') as directory:
        modules = {}
        for name in ('reflex_question', 'direct_call_supervision'):
            path = Path(directory) / (name + '.py')
            path.write_bytes(subprocess.check_output(
                ['git', 'show', 'ab602a2:minebot/app/' + name + '.py'], cwd=root))
            spec = importlib.util.spec_from_file_location('baseline_' + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules[name] = module
        modules['direct_call_supervision'].rq = modules['reflex_question']
        runtime = SimpleNamespace(agent_context=SimpleNamespace(goal_text=''),
                                  trace=SimpleNamespace(emit=lambda *a, **kw: None))
        compilers = (modules['direct_call_supervision'].CallSupervisor(runtime), CallSupervisor(runtime))
        with patch('time.monotonic', return_value=fixture['clock_s']):
            for case in cases:
                tool = SimpleNamespace(name=case['execution']['tool'])
                record = {'tool': tool.name, 'params': case['params'],
                          'supervision_context': case['context']}
                requests = []
                for compiler in compilers:
                    options = compiler._options_for(tool, record, execution=case['execution'])
                    request = compiler._question(str(case['seq']), tool, case['params'], case['execution'],
                                                 idle_s=0, purpose='running_frame',
                                                 supervision_context=case['context'], options=options)
                    request = {k: v for k, v in request.items() if not k.startswith('_request_chars')}
                    requests.append(request)
                assert requests[0] == case['old_request'], case['seq']
                expected = copy.deepcopy(requests[0])
                intent = expected['state'].get('agent_intent')
                if intent:
                    expected['state']['agent_intent'] = f'The Agent wants the following result: “{intent}”.'
                    expected['questions']['q']['instructions'] = (
                        "Choose what the Body does right now to achieve the Agent's intent."
                    )
                facts=case['execution']['facts']
                selected=facts.get('selected_item')
                if isinstance(selected,str) and facts.get('selected_item_known') is not False:
                    expected['state']['body']+=' The main hand holds '+(selected or 'nothing')+'.'
                # Permit only the two verified intent clarifications and the
                # observed main hand. All other facts/options remain exact.
                assert json.dumps(expected, ensure_ascii=False, separators=(',', ':')) == json.dumps(
                    requests[1], ensure_ascii=False, separators=(',', ':')), case['seq']
