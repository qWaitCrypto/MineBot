import importlib.util
import json
from pathlib import Path
import time

root = Path('/home/qwait/MineBot-always-on')
run = root / 'logs/reflex-always-on-20261014-step7-fixed-g'
spec = importlib.util.spec_from_file_location('fixture', root / 'logs/step3-current-live.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

def rows():
    data = []
    for line in (run / 'trace.jsonl').open():
        try: data.append(json.loads(line))
        except ValueError: pass
    return data

def marker(kind, **kw):
    record = {'time': time.time(), 'kind': kind, 'last_seq': rows()[-1]['seq'], **kw}
    with (run / 'fixture-events.jsonl').open('a') as out:
        out.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(json.dumps(record, ensure_ascii=False), flush=True)

marker('life_acceptance_ended_early', reason='Home navigation did not reach its destination; repeated look/stay switching. Not a 30-minute acceptance pass.')
origin = rows()[-1]['seq']
marker('controlled_death_diagnostic', command='kill Step3CurrentBot', response=fixture.rcon('kill Step3CurrentBot'))
deadline = time.monotonic() + 50
while time.monotonic() < deadline:
    data = [r for r in rows() if r['seq'] > origin]
    if any(r['event'] == 'reflex_respawned' for r in data):
        break
    time.sleep(.25)
data = [r for r in rows() if r['seq'] > origin]
selected = [r for r in data if r['event'] in ('execution_cancel_started','execution_cancelled',
    'reflex_death','reflex_respawned','body_event_wake','reflex_state_changed')]
(run / 'controlled-death-evidence.json').write_text(json.dumps(selected, ensure_ascii=False, indent=2))
for r in selected:
    print(r['seq'], r['event'], {k: r[k] for k in ('phase_ms','elapsed_ms','reason','kind') if k in r}, flush=True)
marker('controlled_death_diagnostic_ended', reflex_deaths=sum(r['event']=='reflex_death' for r in data),
    reflex_respawns=sum(r['event']=='reflex_respawned' for r in data))
