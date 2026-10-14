"""Step-5 fixture: melee zombie with a sword, far zombie, low health + skeleton (artificial, labelled).

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


def running_work(r, t):
    return (r.get('event') == 'call_supervision_request' and r.get('purpose') == 'running_frame'
            and r.get('tool') in ('collect_resource', 'mine_block_collect', 'craft_item')
            and ((r.get('frame_input') or {}).get('execution') or {}).get('elapsed_s', 0) > 2)


def main():
    trace = Trace()
    note('start', bot=BOT)
    for c in ('gamerule minecraft:spawn_mobs false', 'gamerule minecraft:advance_time false',
              'difficulty easy', 'time set night', 'gamerule minecraft:natural_health_regeneration false'):
        rcon(c)
    # 1. Zombie next to the Body, iron sword in hand, full health.
    if wait(trace, running_work, 900):
        rcon('item replace entity %s weapon.mainhand with minecraft:iron_sword' % BOT)
        rcon('execute at %s run summon minecraft:zombie ~1 ~ ~ {PersistenceRequired:1b,Tags:["s5_melee"]}' % BOT)
        note('scene', label='1_melee_sword')
        time.sleep(30)
        rcon('kill @e[tag=s5_melee]')
        note('scene_end', label='1_melee_sword')
    # 2. Zombie 20 blocks away, NoAI, no injury: work should continue.
    if wait(trace, running_work, 600):
        rcon('execute at %s run summon minecraft:zombie ~20 ~ ~ {NoAI:1b,PersistenceRequired:1b,Tags:["s5_far"]}' % BOT)
        note('scene', label='2_far_zombie')
        time.sleep(15)
        rcon('kill @e[tag=s5_far]')
        note('scene_end', label='2_far_zombie')
    # 3. Low health and a skeleton 8 blocks away.
    if wait(trace, running_work, 600):
        # Full health first: natural regeneration is off, so earlier scenes
        # leave less than 14 and the damage alone killed the Body (claude3).
        rcon('effect give %s minecraft:instant_health 1 10 true' % BOT)
        time.sleep(1.5)
        rcon('damage %s 14 minecraft:generic' % BOT)
        rcon('execute at %s run summon minecraft:skeleton ~8 ~ ~ {PersistenceRequired:1b,Tags:["s5_ranged"]}' % BOT)
        note('scene', label='3_low_health_skeleton')
        time.sleep(25)
        rcon('kill @e[tag=s5_ranged]')
        rcon('effect give %s minecraft:instant_health 1 10 true' % BOT)
        note('scene_end', label='3_low_health_skeleton')
    note('done')


try:
    main()
finally:
    for c in ('kill @e[tag=s5_melee]', 'kill @e[tag=s5_far]', 'kill @e[tag=s5_ranged]', 'time set day',
              'gamerule minecraft:advance_time true', 'gamerule minecraft:spawn_mobs true',
              'gamerule minecraft:natural_health_regeneration true', 'difficulty normal'):
        rcon(c)
    for box in placed_boxes:
        drain(box, 'final_cleanup')
    for target in placed_blocks:
        rcon('execute if block %d %d %d minecraft:stone run setblock %d %d %d minecraft:air' % (target + target))
