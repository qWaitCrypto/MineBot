"""Existing isolated launcher, retained world, current committed source."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

ROOT = Path('/home/qwait/MineBot-always-on')
RUN = ROOT / 'logs/reflex-always-on-20261014-step7-fixed-g'
SERVER = Path('/tmp/minebot-reflex-options-server')
assert SERVER.is_dir()
for port in (25569, 25579, 8771):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
props = dict(line.split('=', 1) for line in (SERVER / 'server.properties').read_text().splitlines()
             if '=' in line and not line.startswith('#'))
assert props['server-port'] == '25569' and props['rcon.port'] == '25579'
RUN.mkdir(exist_ok=False)
env = {k: v for k, v in os.environ.items() if not k.startswith(('MINEBOT_', 'OPENAI_', 'ANTHROPIC_', 'RCON'))}
with (RUN / 'server-launch.log').open('wb') as log:
    server = subprocess.Popen(['/usr/lib/jvm/java-25-openjdk-amd64/bin/java', '-Xms256M', '-Xmx1G',
        '-jar', 'fabric-server-launch.jar', 'nogui'], cwd=SERVER, env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    if server.poll() is not None:
        raise RuntimeError('Test server exited; inspect run server-launch.log')
    try:
        with socket.create_connection(('127.0.0.1', 25579), timeout=.5):
            break
    except OSError:
        time.sleep(.5)
else:
    raise RuntimeError('Test server not ready')
profile = json.loads((ROOT / 'logs/reflex-always-on-20261014-step7-current-f/life-profile.json').read_text())
assert profile['world'] == props['level-name']
profile_path = RUN / 'life-profile.json'
profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2))
config = Path(tempfile.mkdtemp(prefix='minebot-step7-fixed-config-'))
(config / 'minebot').mkdir()
(config / 'minebot/servers.toml').write_text('')
env.update({'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0', 'XDG_RUNTIME_DIR': '/mnt/wslg/runtime-dir',
    'PULSE_SERVER': 'unix:/mnt/wslg/PulseServer', 'TERM': 'xterm-256color', 'XDG_CONFIG_HOME': str(config),
    'MINEBOT_CLIENT_LAUNCH': f'bash {ROOT}/logs/step2-live-client.sh',
    'MINEBOT_AGENT_KERNEL': 'sdk', 'MINEBOT_AGENT_STATE_DB': str(RUN / 'state.sqlite3'),
    'MINEBOT_AGENT_LOG_PATH': str(RUN / 'trace.jsonl'), 'MINEBOT_REAL_WORLD_ID': props['level-name'],
    'MINEBOT_CLIENT_FREE_CURSOR': 'true', 'MINEBOT_LIFE_PROFILE': str(profile_path),
    'MINEBOT_DECISION_URL': 'http://117.176.14.165:10004/v1/systemone'})
commands = RUN / 'control-stdin.txt'
commands.touch()
with (RUN / 'launcher-console.log').open('wb') as log:
    launcher = subprocess.Popen(['/home/qwait/MineBot/.venv/bin/python', '-m', 'minebot.shell.app',
        '--env-file', '/home/qwait/.config/minebot/provider-deepseek-v4.1-flash.env',
        '--server', '127.0.0.1:25569', '--bot', 'Step3CurrentBot', '--port', '8771', '--disposable',
        '--run-dir', str(RUN), '--plain', '--foreground'], cwd=ROOT, env=env, stdin=subprocess.PIPE,
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
record = {'code_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
    'code_dirty': False, 'world': props['level-name'], 'fresh_world': False, 'empty_inventory': False,
    'ports': [25569, 25579, 8771], 'bot': 'Step3CurrentBot', 'server_pid': server.pid,
    'launcher_pid': launcher.pid, 'owner_pid': os.getpid(), 'started_at': time.time(),
    'model': 'deepseek-v4.1-flash', 'kernel': 'sdk', 'goal': None,
    'scope': 'Retained world/player; no initial task; isolated config; natural mobs enabled.'}
(RUN / 'acceptance-run.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
print(json.dumps(record), flush=True)
with commands.open() as source:
    while launcher.poll() is None:
        line = source.readline()
        if line:
            launcher.stdin.write(line.encode())
            launcher.stdin.flush()
        else:
            time.sleep(.2)
(RUN / 'launcher-returncode.json').write_text(json.dumps({'exit_code': launcher.returncode}))
