"""Copy redacted event evidence and executable fixture scripts, never provider envs."""
import json
import shutil
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
main = Path('/home/qwait/MineBot/docs/references/reflex-always-on-20261012')
dest = main / 'evidence-next-step'
dest.mkdir(exist_ok=True)
scripts = ('start-step5-next.py', 'control-step5-next.py', 'start-step5-next-b.py', 'control-step5-next-b.py',
    'start-step5-next-c.py', 'control-step5-next-c.py', 'start-step5-next-d.py', 'control-step5-next-d.py',
    'start-step6-next.py', 'control-step6-next.py', 'start-step6-backup-next.py', 'control-step6-backup-next.py',
    'start-step7-next.py', 'control-step7-next.py', 'collect-step5-next-evidence.py',
    'inspect-step6-next.py', 'inspect-step6-backup-next.py', 'running-perception-old-baseline.py',
    'step3-current-live.py', 'start-life-fixed-g.py', 'step2-live-client.sh',
    'collect-step7-next-evidence.py', 'stop-step7-next.py')
for name in scripts:
    shutil.copy2(root / 'logs' / name, dest / name)
for name in ('step5-running-perception-tests.log', 'step5-running-perception-boundary-tests.log',
             'step5-running-perception-tests-v2.log', 'step5-running-perception-tests-v3.log',
             'step5-running-perception-old-failure.log', 'step5-next-all-evidence.log',
             'step6-next-inspect.log', 'step6-backup-next-inspect.log'):
    shutil.copy2(root / 'logs' / name, dest / name)
for name in ('step3-vital-history-trial-edc89ff-v2.summary.json', 'step3-vital-history-trial-edc89ff-v2.jsonl'):
    shutil.copy2(root / 'logs' / name, dest / name)
for stage in ('step5-next', 'step5-next-b', 'step5-next-c', 'step5-next-d', 'step6-next', 'step6-backup-next', 'step7-next'):
    run = root / ('logs/reflex-always-on-20261014-' + stage)
    target = dest / stage
    target.mkdir(exist_ok=True)
    for name in ('acceptance-run.json', 'fixture-events.jsonl', 'acceptance-events.json', 'metrics.json', 'launcher-returncode.json',
                 'disconnect-events.json', 'saved-player-state.json', 'repeat-evidence.json'):
        if (run / name).exists():
            shutil.copy2(run / name, target / name)
    rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
    deliveries = [r for r in rows if r['event'].startswith('reflex_fact_') or r['event'] == 'body_event_wake']
    (target / 'all-delivery-events.json').write_text(json.dumps(deliveries, ensure_ascii=False, indent=2))
    print(stage, [(r['seq'], r['event'], r.get('reason')) for r in deliveries])
target = dest / 'final-suite-7e4c1e6'
target.mkdir(exist_ok=True)
for name in ('pytest.log', 'exit.json'):
    shutil.copy2(root / 'logs/final-suite-7e4c1e6' / name, target / name)
client_lines = (root/'logs/reflex-always-on-20261014-step7-next/client.log').read_text().splitlines()
(dest/'step7-next/client-disconnect.log').write_text('\n'.join(
    line for line in client_lines if 'disconnected with reason' in line or 'Failed to request yggdrasil public key' in line)+'\n')
server_lines = Path('/tmp/minebot-reflex-options-server/logs/latest.log').read_text().splitlines()
(dest/'step7-next/server-disconnect.log').write_text('\n'.join(
    line for line in server_lines if '[10:27:20]' in line)+'\n')
shutil.copy2(root / 'logs/package-next-step-evidence.py', dest / 'package-next-step-evidence.py')
