"""Step-6 fixture: a stone sword in the bag, then an emptied main hand while a plan with interrupt_when runs.

Original step-2 notes follow.

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
    def plan_running(r, t):
        ctx = ((r.get('compiler_input') or {}).get('supervision_context') or {})
        return (r.get('event') == 'call_supervision_request' and r.get('purpose') == 'running_frame'
                and ctx.get('interrupt_when') and ((r.get('frame_input') or {}).get('execution') or {}).get('elapsed_s', 0) > 2)
    if not wait(trace, plan_running, 900):
        note('skip', label='interrupt', reason='no running plan frame with interrupt_when')
        return
    # The sword goes to a main-inventory slot, never the selected hotbar slot
    # (claude2: it landed in the hand and "empty the hand" deleted it). The
    # fresh Body is empty-handed, so the Agent's condition now holds.
    rcon('item replace entity %s inventory.0 with minecraft:stone_sword' % BOT)
    note('sword_in_bag')
    hit = wait(trace, lambda r, t: r.get('event') == 'call_supervision_result' and r.get('choice') == 'interrupt', 30)
    if hit:
        note('interrupt_chosen', seq=hit.get('seq'))
    wait(trace, lambda r, t: r.get('event') == 'reflex_fact_wake', 240)
    note('done')


try:
    main()
finally:
    for box in placed_boxes:
        drain(box, 'final_cleanup')
    for target in placed_blocks:
        rcon('execute if block %d %d %d minecraft:stone run setblock %d %d %d minecraft:air' % (target + target))
