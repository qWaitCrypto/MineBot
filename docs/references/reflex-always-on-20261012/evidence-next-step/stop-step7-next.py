import json
import subprocess
import time
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
run = root / 'logs/reflex-always-on-20261014-step7-next'
subprocess.run(['pkill', '-TERM', '-f', '^/home/qwait/MineBot/.venv/bin/python logs/control-step7-next.py$'], check=False)
data = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
with (run/'fixture-events.jsonl').open('a') as out:
    out.write(json.dumps({'time':time.time(), 'kind':'validation_stopped_after_disconnect',
        'last_seq':data[-1]['seq'], 'reason':'Client left the world; synthetic death health is null. Not a 30-minute life pass.'})+'\n')
with (run/'control-stdin.txt').open('a') as out:
    out.write('/quit\n')
print('Stopped only the owned fixture controller; /quit sent to owned session.')
