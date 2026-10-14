import importlib.util
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

root = Path('/home/qwait/MineBot-always-on')
docs = Path('/home/qwait/MineBot/docs/references/reflex-always-on-20261012')
dest = docs / 'evidence-connection-20261014'
dest.mkdir(exist_ok=True)
spec = importlib.util.spec_from_file_location('metrics', docs / 'metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
reports = []
for suffix in ('disconnect-check', 'disconnect-fixed', 'reconnect-check'):
    run = root / ('logs/reflex-always-on-20261014-' + suffix)
    rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
    summary = metrics.summarize(run)
    (run / 'metrics.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    analysis = {'run': suffix, 'scope': 'controlled server kick' if suffix != 'reconnect-check' else 'fresh client/session rejoin of same world and real Agent /goal',
                **{k: summary[k] for k in ('elapsed_s', 'requests_by_purpose', 'answers', 'decision_ms_median', 'agent_delivery_counts', 'agent_deliveries', 'delegate_work', 'read_reflex', 'deaths', 'supervised_body_call_s')}}
    native = []
    for line in (run / 'client.log').read_text().splitlines():
        marker = 'MINEBOT_LIFECYCLE '
        if marker in line:
            native.append(json.loads(line.split(marker, 1)[1]))
    (run / 'native-lifecycle.json').write_text(json.dumps(native, ensure_ascii=False, indent=2))
    if suffix.startswith('disconnect-'):
        fixture = [json.loads(line) for line in (run / 'fixture-events.jsonl').read_text().splitlines()]
        kicked = next(e for e in fixture if e['kind'] == 'server_kick')
        start = next(e for e in fixture if e['kind'] == 'disconnect_begin')
        observation = next(e for e in fixture if e['kind'] == 'disconnect_observation')
        after = [r for r in rows if r['seq'] > start['last_seq'] and r['ts'] < observation['time']]
        stops = [r for r in after if r['event'] == 'reflex_loop_stopped']
        analysis['after_kick_requests'] = [r['seq'] for r in after if r['event'] == 'call_supervision_request']
        analysis['after_kick_stops'] = stops
        analysis['observation_s'] = observation['time'] - kicked['time']
        analysis['stop_after_command_return_ms'] = (stops[0]['ts'] - kicked['time']) * 1000 if stops else None
        disconnect = next((e for e in native if e['name'] == 'world_disconnected'), None)
        analysis['native_disconnect_to_python_stop_ms'] = ((stops[0]['ts'] * 1000 - disconnect['data']['sampled_at_ms'])
                                                         if stops and disconnect else None)
    out = dest / suffix
    out.mkdir(exist_ok=True)
    for name in ('acceptance-run.json', 'launcher-returncode.json', 'fixture-events.jsonl', 'metrics.json', 'native-lifecycle.json', 'trace.jsonl'):
        shutil.copy2(run / name, out / name)
    analysis['launcher_exit'] = json.loads((run / 'launcher-returncode.json').read_text())['exit_code']
    reports.append(analysis)
for name in ('connection-before.log', 'connection-targeted-final.log', 'connection-java.log',
             'connection-old-regression.py', 'connection-java.sh',
             'start-disconnect-check.py', 'control-disconnect-check.py', 'start-disconnect-fixed.py',
             'control-disconnect-fixed.py', 'start-reconnect-check.py', 'control-reconnect-check.py',
             'collect-connection-evidence.py'):
    shutil.copy2(root / 'logs' / name, dest / name)
java = [ET.parse(p).getroot() for p in (root / 'minecraft/client/body/build/test-results/test').glob('TEST-*.xml')]
result = {'runs': reports, 'java_tests': {key: sum(int(xml.attrib.get(key, 0)) for xml in java)
                                        for key in ('tests', 'failures', 'errors', 'skipped')}}
(dest / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False, indent=2))
