"""A real user read task during a life action; no injected Body/Decision call."""
import importlib.util
import json
from pathlib import Path
import time

root = Path('/home/qwait/MineBot-always-on')
run = root / 'logs/reflex-always-on-20261014-step7-fixed-h'
spec = importlib.util.spec_from_file_location('fixture', root / 'logs/step3-current-live.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

def rows():
    data = []
    if (run / 'trace.jsonl').exists():
        for line in (run / 'trace.jsonl').open():
            try: data.append(json.loads(line))
            except ValueError: pass
    return data

def mark(kind, **kw):
    data = rows()
    event = {'time': time.time(), 'kind': kind, 'last_seq': data[-1]['seq'] if data else None, **kw}
    with (run / 'fixture-events.jsonl').open('a') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')
    print(json.dumps(event, ensure_ascii=False), flush=True)

deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    data = rows()
    body = next((r for r in reversed(data) if r['event'] == 'body_progress' and r.get('pos')), None)
    if body and any(r['event']=='call_supervision_request' for r in data):
        break
    time.sleep(.5)
else:
    raise RuntimeError('No current live body frame')
x, y, z = body['pos']
mark('visitor_for_read_window', response=fixture.rcon(f'player Visitor spawn at {x+6:.1f} {y:.1f} {z:.1f}'))
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    data = rows()
    current = next((r for r in reversed(data) if r['event']=='call_supervision_request'), {})
    if current.get('purpose')=='running_frame' and current.get('loop_state')=='idle' and time.time()-current['ts']<.6:
        text='请只读取当前背包，告诉我有什么。不要移动、采集、合成、放置或破坏方块；回答后回到空闲生活。'
        with (run / 'control-stdin.txt').open('a') as f:
            f.write(text+'\n')
        mark('agent_read_task_during_life', text=text, life_request_seq=current['seq'], call_id=current['tool_call_id'])
        break
    time.sleep(.1)
else:
    mark('read_window_unavailable')
