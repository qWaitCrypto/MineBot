import json
import os
from pathlib import Path
import subprocess
import time

root = Path('/home/qwait/MineBot-always-on')
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
assert commit.startswith('0a2840b')
assert not subprocess.check_output(['git', 'diff', '--name-only'], cwd=root, text=True).strip()
run = root / 'logs/final-suite-0a2840b'
run.mkdir(exist_ok=False)
env = {k:v for k,v in os.environ.items() if not k.startswith(('MINEBOT_', 'OPENAI_', 'ANTHROPIC_', 'RCON'))}
with (run / 'pytest.log').open('wb') as log:
    process = subprocess.Popen(['timeout', '2700', '/home/qwait/MineBot/.venv/bin/python',
                                '-m', 'pytest', 'tests/unit', '-q'], cwd=root, env=env,
                               stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               start_new_session=True)
(run / 'running.json').write_text(json.dumps({'commit':commit,'pid':process.pid,'owner_pid':os.getpid(),
    'started_at':time.time(),'command':'pytest tests/unit -q','concurrent_game_run':'life-30-final'}, indent=2))
print(json.dumps({'pid':process.pid,'commit':commit,'log':str(run / 'pytest.log')}),flush=True)
code = process.wait()
dirty = subprocess.check_output(['git', 'diff', '--name-only'], cwd=root, text=True).splitlines()
(run / 'exit.json').write_text(json.dumps({'exit_code':code,'ended_at':time.time(),
    'commit':commit,'tracked_source_dirty_at_end':dirty},indent=2))
print(json.dumps({'exit_code':code}),flush=True)
