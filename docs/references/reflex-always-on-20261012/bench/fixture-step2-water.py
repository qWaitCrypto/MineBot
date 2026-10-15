"""Step-2 water fixtures driven by the live trace (artificial scenes, labelled).

A. Agent thinking (between calls, an llm turn open, loop idle): water around the Body.
B. Work running (collect_resource running frame, elapsed > 3 s): water around the Body.
C. During the protective surfacing that follows B: one stone above the head for 12 s.

Only air is replaced (fill ... replace air / setblock ... keep); cleanup removes
only the water/stone this script placed.  Every action goes to fixture-events.jsonl.
"""
import json, math, subprocess, sys, time
from pathlib import Path

RUN = Path(sys.argv[1])
BOT = sys.argv[2]
RCON = '/home/qwait/MineBot/docs/references/reflex-always-on-20261012/live/reflex-rcon.py'
LOG = RUN / 'fixture-events.jsonl'
TRACE = RUN / 'trace.jsonl'
placed_boxes, placed_blocks = [], []


def note(kind, **data):
    with LOG.open('a') as sink:
        sink.write(json.dumps({'ts': time.time(), 'kind': kind, **data}, ensure_ascii=False) + '\n')


def rcon(command):
    out = subprocess.run([sys.executable, RCON, command], capture_output=True, text=True, timeout=20)
    note('rcon', command=command, out=(out.stdout or out.stderr).strip()[:300])
    return out.stdout


class Trace:
    def __init__(self):
        self.offset = 0
        self.rows = []

    def poll(self):
        if not TRACE.exists():
            return []
        with TRACE.open() as source:
            source.seek(self.offset)
            new = []
            for line in source:
                if not line.endswith('\n'):
                    break
                self.offset += len(line.encode())
                try:
                    new.append(json.loads(line))
                except Exception:
                    pass
        self.rows += new
        return new


def position(trace):
    for row in reversed(trace.rows):
        if row.get('event') in ('body_progress', 'body_state'):
            pos = row.get('pos') or row.get('position')
            if isinstance(pos, list) and len(pos) == 3:
                return [math.floor(float(v)) for v in pos]
    return None


def flood(trace, label):
    pos = position(trace)
    if not pos:
        return None
    x, y, z = pos
    box = (x - 1, y, z - 1, x + 1, y + 3, z + 1)
    rcon('fill %d %d %d %d %d %d minecraft:water replace air' % box)
    placed_boxes.append(box)
    note('flood', label=label, pos=pos, box=box, seq=trace.rows[-1].get('seq') if trace.rows else None)
    return box


def drain(box, label):
    rcon('fill %d %d %d %d %d %d minecraft:air replace minecraft:water' % box)
    note('drain', label=label, box=box)


def wait(trace, predicate, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for row in trace.poll():
            if predicate(row, trace):
                return row
        time.sleep(0.3)
    return None


def main():
    trace = Trace()
    note('start', bot=BOT)
    # Wait for the Agent to have ended one Body call and to be thinking.
    state = {'tool_end': False}

    def thinking(row, tr):
        if row.get('event') == 'tool_end':
            state['tool_end'] = True
        return state['tool_end'] and row.get('event') == 'llm_start'
    if '--skip-a' not in sys.argv and wait(trace, thinking, 600):
        box = flood(trace, 'A_agent_thinking')
        wait(trace, lambda r, t: r.get('event') == 'call_supervision_result' and r.get('purpose') == 'default_result_frame', 90)
        time.sleep(3)
        if box:
            drain(box, 'A_agent_thinking')
    else:
        note('skip', label='A_agent_thinking', reason='no thinking window seen')
    # B: flood a running collect call.
    row = wait(trace, lambda r, t: r.get('event') == 'call_supervision_request' and r.get('purpose') == 'running_frame'
               and r.get('tool') == 'collect_resource' and ((r.get('frame_input') or {}).get('execution') or {}).get('elapsed_s', 0) > 3, 900)
    if not row:
        note('skip', label='B_work_running', reason='no running collect seen')
        return
    box = flood(trace, 'B_work_running')
    # C: when the protective surfacing runs, block the head once for 12 s.
    surf = wait(trace, lambda r, t: r.get('event') == 'call_supervision_request' and r.get('purpose') == 'running_frame'
                and r.get('tool') == 'go_to_surface', 60)
    if surf:
        pos = position(trace)
        if pos:
            x, y, z = pos
            target = (x, y + 2, z)
            # The cell above the head is usually the fixture's own water.
            out = rcon('setblock %d %d %d minecraft:stone keep' % target)
            if 'Could not' in out:
                rcon('execute if block %d %d %d minecraft:water run setblock %d %d %d minecraft:stone' % (target + target))
            placed_blocks.append(target)
            note('ceiling', target=target, seq=surf.get('seq'))
            time.sleep(12)
            rcon('execute if block %d %d %d minecraft:stone run setblock %d %d %d minecraft:air' % (target + target))
            note('ceiling_removed', target=target)
    else:
        note('skip', label='C_ceiling', reason='no surfacing frame within 60 s')
    wait(trace, lambda r, t: r.get('event') == 'call_supervision_result' and r.get('purpose') == 'default_result_frame', 120)
    time.sleep(20)
    if box:
        drain(box, 'B_work_running')
    note('done')


try:
    main()
finally:
    for box in placed_boxes:
        drain(box, 'final_cleanup')
    for target in placed_blocks:
        rcon('execute if block %d %d %d minecraft:stone run setblock %d %d %d minecraft:air' % (target + target))
