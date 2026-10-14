"""Authorized current-source Agent acceptance; explicit fixture ownership."""
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

ROOT = Path('/home/qwait/MineBot-always-on')
RUN = ROOT / 'logs/reflex-always-on-20261014-step3-current'
SERVER = Path('/tmp/minebot-reflex-options-server')
BOT = 'Step3CurrentBot'


def rcon(command):
    props = dict(line.split('=', 1) for line in (SERVER / 'server.properties').read_text().splitlines() if '=' in line and not line.startswith('#'))
    assert props['server-port'] == '25569' and props['rcon.port'] == '25579'
    with socket.create_connection(('127.0.0.1', 25579), timeout=5) as connection:
        def send(identifier, kind, text):
            payload = struct.pack('<ii', identifier, kind) + text.encode() + b'\0\0'
            connection.sendall(struct.pack('<i', len(payload)) + payload)
            def receive(count):
                data = b''
                while len(data) < count:
                    part = connection.recv(count - len(data))
                    if not part:
                        raise EOFError('RCON closed')
                    data += part
                return data
            length = struct.unpack('<i', receive(4))[0]
            data = receive(length)
            return struct.unpack('<i', data[:4])[0], data[8:-2].decode(errors='replace')
        assert send(1, 3, props['rcon.password'])[0] == 1
        return send(2, 2, command)[1]


def rows():
    result = []
    path = RUN / 'trace.jsonl'
    if path.exists():
        for line in path.open():
            try:
                result.append(json.loads(line))
            except ValueError:
                pass
    return result


def start():
    assert SERVER.is_dir()
    for port in (25569, 25579, 8771):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    RUN.mkdir(exist_ok=False)
    properties = SERVER / 'server.properties'
    original = properties.read_text()
    backup = SERVER / 'server.properties.before-step3-current-6db65ec'
    assert not backup.exists()
    backup.write_text(original)
    backup.chmod(0o600)
    world = 'step3-current-6db65ec-20261014'
    assert not (SERVER / world).exists()
    updates = {'level-name': world, 'level-seed': '1', 'server-port': '25569', 'rcon.port': '25579', 'enable-rcon': 'true'}
    lines = []
    for line in original.splitlines():
        key = line.split('=', 1)[0]
        lines.append(key + '=' + updates.pop(key) if key in updates else line)
    lines += [key + '=' + value for key, value in updates.items()]
    properties.write_text('\n'.join(lines) + '\n')
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('MINEBOT_', 'OPENAI_', 'ANTHROPIC_', 'RCON'))}
    with (RUN / 'server-launch.log').open('wb') as log:
        server = subprocess.Popen(['/usr/lib/jvm/java-25-openjdk-amd64/bin/java', '-Xms256M', '-Xmx1G', '-jar', 'fabric-server-launch.jar', 'nogui'],
                                  cwd=SERVER, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError('Test server exited; inspect its log')
        try:
            with socket.create_connection(('127.0.0.1', 25579), timeout=.5):
                break
        except OSError:
            time.sleep(.5)
    else:
        raise RuntimeError('Test server did not start')
    # No provider-file contents are read by this harness. The normal launcher
    # loads the named private provider file and writes its redacted manifest.
    environment.update({'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0', 'XDG_RUNTIME_DIR': '/mnt/wslg/runtime-dir',
                        'PULSE_SERVER': 'unix:/mnt/wslg/PulseServer', 'TERM': 'xterm-256color',
                        'MINEBOT_CLIENT_LAUNCH': f'bash {ROOT}/logs/step2-live-client.sh', 'MINEBOT_AGENT_KERNEL': 'sdk',
                        'MINEBOT_AGENT_STATE_DB': str(RUN / 'state.sqlite3'), 'MINEBOT_AGENT_LOG_PATH': str(RUN / 'trace.jsonl'),
                        'MINEBOT_REAL_WORLD_ID': world, 'MINEBOT_CLIENT_FREE_CURSOR': 'true',
                        'MINEBOT_DECISION_URL': 'http://117.176.14.165:10004/v1/systemone'})
    goal = '从空背包开始自主搜集材料，制作并装备一把石镐，然后采集至少4份生铁。自己决定身体工具、调用顺序和工作计划；不要传送，不要外部补给。最后如实报告实际结果。'
    commands = RUN / 'control-stdin.txt'
    commands.touch()
    with (RUN / 'launcher-console.log').open('wb') as log:
        launcher = subprocess.Popen(['/home/qwait/MineBot/.venv/bin/python', '-m', 'minebot.shell.app',
                         '--env-file', '/home/qwait/.config/minebot/provider-deepseek-v4.1-flash.env',
                         '--server', '127.0.0.1:25569', '--bot', BOT, '--port', '8771', '--disposable',
                         '--run-dir', str(RUN), '--plain', '--foreground', goal], cwd=ROOT, env=environment,
                         stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    record = {'code_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'world': world, 'fresh_world': True, 'empty_inventory': True, 'ports': [25569, 25579, 8771],
              'bot': BOT, 'server_pid': server.pid, 'launcher_pid': launcher.pid, 'owner_pid': os.getpid(),
              'started_at': time.time(), 'model': 'deepseek-v4.1-flash', 'kernel': 'sdk', 'goal': goal}
    (RUN / 'acceptance-run.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False), flush=True)
    with commands.open() as source:
        while launcher.poll() is None:
            line = source.readline()
            if line:
                launcher.stdin.write(line.encode())
                launcher.stdin.flush()
            else:
                time.sleep(.2)
    (RUN / 'launcher-returncode.json').write_text(json.dumps({'exit_code': launcher.returncode}))


def status():
    from collections import Counter
    data = rows()
    if not data:
        print('No trace yet')
        return
    print('events', len(data), 'duration_s', round(data[-1]['ts'] - data[0]['ts'], 1), 'age_s', round(time.time() - data[-1]['ts'], 1))
    print('answers', dict(Counter((r.get('purpose'), r.get('choice')) for r in data if r['event'] == 'call_supervision_result')))
    relevant = [r for r in data if r['event'] in ('provider_manifest', 'tool_invoke', 'reflex_state_changed', 'call_supervision_default_requested',
                    'call_supervision_default_finished', 'reflex_fact_wake', 'body_event_wake', 'reflex_death', 'interactive_ready', 'agent_error')]
    for r in relevant[-12:]:
        print(r['seq'], r['event'], {k: r[k] for k in ('tool', 'loop_state', 'from', 'to', 'cause', 'choice', 'reason', 'kind', 'model', 'kernel') if k in r})
    state = next((r for r in reversed(data) if r['event'] == 'body_progress' and 'health' in r), None)
    if state:
        print('body', state['seq'], {k: state[k] for k in ('pos', 'health', 'food', 'oxygen', 'inventory', 'inventory_counts', 'selected_item') if k in state})


def flood():
    """Test intervention only; no injected Agent/runtime/Decision choices."""
    def marker(kind, **details):
        data = rows()
        event = {'time': time.time(), 'last_seq': data[-1]['seq'] if data else None, 'kind': kind, **details}
        with (RUN / 'fixture-events.jsonl').open('a') as output:
            output.write(json.dumps(event, ensure_ascii=False) + '\n')
        print(json.dumps(event, ensure_ascii=False), flush=True)
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        data = rows()
        question = next((r for r in reversed(data) if r['event'] == 'call_supervision_request'), None)
        # First let one collection settle, preserving a normal-work control.
        control = any(r['event'] == 'call_supervision_finished' and r.get('tool') == 'collect_resource' for r in data)
        if question and control and question.get('purpose') == 'running_frame' and question.get('tool') == 'collect_resource' and time.time() - question['ts'] < 1.5:
            facts = question.get('compiler_input', {}).get('facts') or question.get('frame_input', {}).get('execution', {}).get('facts', {})
            position = facts.get('position', facts.get('pos'))
            if position:
                break
        time.sleep(.2)
    else:
        marker('work_flood_unavailable')
        return
    x, y, z = map(math.floor, position)
    command = f'fill {x-3} {y-3} {z-3} {x+3} {y+7} {z+3} minecraft:water replace minecraft:air'
    marker('work_flood', request_seq=question['seq'], call_id=question['tool_call_id'], position=position, response=rcon(command))
    origin = question['seq']
    deadline = time.monotonic() + 100
    try:
        while time.monotonic() < deadline:
            recent = [r for r in rows() if r['seq'] > origin]
            results = [r for r in recent if r['event'] == 'call_supervision_result' and r.get('purpose') == 'default_result_frame']
            if any(r.get('choice') == 'call_agent' for r in results):
                marker('work_flood_handoff', result_seqs=[r['seq'] for r in results], choices=[r.get('choice') for r in results])
                break
            state = next((r for r in reversed(recent) if r['event'] == 'body_progress' and 'health' in r), None)
            if state and state['health'] < 8:
                marker('work_flood_aborted_health', body_seq=state['seq'], health=state['health'])
                break
            time.sleep(.15)
        else:
            marker('work_flood_deadline', result_seqs=[r['seq'] for r in results], choices=[r.get('choice') for r in results])
    finally:
        clean = f'fill {x-3} {y-3} {z-3} {x+3} {y+7} {z+3} minecraft:air replace minecraft:water'
        marker('water_removed', response=rcon(clean))


def drown():
    data = rows()
    state = next(r for r in reversed(data) if r['event'] == 'body_progress' and r.get('pos'))
    x, y, z = map(math.floor, state['pos'])
    initial_deaths = sum(r['event'] == 'reflex_death' for r in data)
    event = {'kind': 'deliberately_sealed_water_fixture', 'time': time.time(), 'last_seq': data[-1]['seq'],
             'position': state['pos'], 'scope': 'Artificial drowning/respawn transport check; not a survival-navigation acceptance.'}
    with (RUN / 'fixture-events.jsonl').open('a') as output:
        output.write(json.dumps(event) + '\n')
    # Only air/liquid cells in this fresh disposable world are replaced; no
    # terrain or player-placed solid block is destroyed by the fixture.
    faces = [(x-1,y-1,z-1,x-1,y+3,z+1), (x+1,y-1,z-1,x+1,y+3,z+1),
             (x-1,y-1,z-1,x+1,y+3,z-1), (x-1,y-1,z+1,x+1,y+3,z+1),
             (x-1,y+3,z-1,x+1,y+3,z+1), (x-1,y-1,z-1,x+1,y-1,z+1)]
    try:
        for bounds in faces:
            coordinates = ' '.join(map(str, bounds))
            for replaced in ('air', 'water'):
                rcon(f'fill {coordinates} minecraft:bedrock replace minecraft:{replaced}')
        rcon(f'fill {x} {y} {z} {x} {y+2} {z} minecraft:water replace minecraft:air')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            data = rows()
            deaths = [r for r in data if r['event'] == 'reflex_death']
            if len(deaths) > initial_deaths:
                print('Drowning fixture death seq', deaths[-1]['seq'], flush=True)
                break
            time.sleep(.2)
        else:
            print('Drowning fixture did not produce death; no acceptance claimed', flush=True)
    finally:
        rcon(f'fill {x-1} {y-1} {z-1} {x+1} {y+3} {z+1} minecraft:air replace minecraft:bedrock')
        rcon(f'fill {x} {y} {z} {x} {y+2} {z} minecraft:air replace minecraft:water')
        with (RUN / 'fixture-events.jsonl').open('a') as output:
            output.write(json.dumps({'kind': 'sealed_water_fixture_removed', 'time': time.time(), 'last_seq': rows()[-1]['seq']}) + '\n')


if __name__ == '__main__':
    if sys.argv[1] == 'start':
        start()
    elif sys.argv[1] == 'status':
        status()
    elif sys.argv[1] == 'rcon':
        print(rcon(sys.argv[2]))
    elif sys.argv[1] == 'flood':
        flood()
    elif sys.argv[1] == 'drown':
        drown()
