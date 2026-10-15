"""Step-7 fixture: thirty minutes of idle life on a fresh world (seed 1), no initial task.

Interventions (all logged to fixture-events.jsonl, none of them answers):
- a small oak_planks wall beside home stands for player-made blocks (placed only into air;
  every block is checked before and after);
- Visitor (Carpet fake player) walks up to the Body twice and leaves again;
- time is frozen at day, set to dusk once with the Body moved 14 blocks from home;
- while the Body walks home at dusk (or after 90 s of dusk) a task arrives by chat.
"""
import json, math, subprocess, sys, time
from pathlib import Path

RUN = Path(sys.argv[1])
BOT = sys.argv[2]
RCON = '/home/qwait/MineBot/docs/references/reflex-always-on-20261012/live/reflex-rcon.py'
LOG = RUN / 'fixture-events.jsonl'
TRACE = RUN / 'trace.jsonl'
HOME = (147.5, 70, 164.5)
WALL = [(x, y, 168) for x in (150, 151, 152) for y in (70, 71)]
OBSERVE_S = 1800


def note(kind, **data):
    with LOG.open('a') as sink:
        sink.write(json.dumps({'ts': time.time(), 'kind': kind, **data}, ensure_ascii=False) + '\n')


def rcon(command):
    out = subprocess.run([sys.executable, RCON, command], capture_output=True, text=True, timeout=20)
    note('rcon', command=command, out=(out.stdout or out.stderr).strip()[:300])
    return out.stdout


def send(text):
    with (RUN / 'control-stdin.txt').open('a') as out:
        out.write(text + '\n')
    note('user_message', text=text)


class Trace:
    def __init__(self):
        self.offset = 0
        self.rows = []

    def poll(self):
        if not TRACE.exists():
            return []
        with TRACE.open('rb') as source:
            source.seek(self.offset)
            new = []
            for line in source:
                if not line.endswith(b'\n'):
                    break
                self.offset += len(line)
                try:
                    new.append(json.loads(line))
                except Exception:
                    pass
        self.rows += new
        return new


def position(trace):
    for row in reversed(trace.rows):
        if row.get('event') == 'body_progress' and row.get('pos'):
            return row['pos']
    return None


def wall_state():
    return {f'{x} {y} {z}': ('passed' in rcon(f'execute if block {x} {y} {z} minecraft:oak_planks').lower())
            for x, y, z in WALL}


def visitor_walks_up(trace, label):
    pos = position(trace)
    if not pos:
        note('scene_skipped', label=label, reason='no body position')
        return
    x, y, z = pos
    note('scene', label=label, body=pos)
    rcon('player Visitor kill')
    rcon(f'player Visitor spawn at {x + 12:.1f} {y + 3:.1f} {z:.1f}')
    time.sleep(1)
    rcon(f'execute positioned {x + 12:.1f} {y:.1f} {z:.1f} positioned over motion_blocking_no_leaves run tp Visitor ~ ~ ~')
    time.sleep(20)
    pos = position(trace) or pos
    rcon(f'player Visitor look at {pos[0]:.1f} {pos[1] + 1.6:.1f} {pos[2]:.1f}')
    rcon('player Visitor move forward')
    time.sleep(2)
    rcon('player Visitor stop')
    note('visitor_near', label=label)


def main():
    trace = Trace()
    note('start', bot=BOT)
    deadline = time.time() + 600
    while time.time() < deadline:
        trace.poll()
        if any(r.get('event') == 'call_supervision_request' for r in trace.rows):
            break
        time.sleep(1)
    else:
        note('abort', reason='no question within 600 s')
        return
    for command in ('time set day', 'gamerule minecraft:advance_time false', 'gamerule minecraft:spawn_mobs true',
                    'gamerule minecraft:natural_health_regeneration true', f'tp {BOT} {HOME[0]} {HOME[1]} {HOME[2]}'):
        rcon(command)
    for x, y, z in WALL:
        rcon(f'setblock {x} {y} {z} minecraft:oak_planks keep')
    before = wall_state()
    note('life_observation_started', home=HOME, wall_before=before)
    start = time.time()
    done = set()
    while time.time() - start < OBSERVE_S:
        trace.poll()
        t = time.time() - start
        if (RUN / 'launcher-returncode.json').exists():
            note('ended_early', reason='launcher exited', t=t)
            break
        if t >= 120 and 'visitor1' not in done:
            done.add('visitor1')
            visitor_walks_up(trace, 'visitor_first')
        if t >= 230 and 'visitor1_leave' not in done:
            done.add('visitor1_leave')
            rcon('player Visitor kill')
            note('scene_end', label='visitor_first')
        if t >= 330 and 'dusk' not in done:
            done.add('dusk')
            rcon(f'execute positioned {HOME[0] + 14} {HOME[1]} {HOME[2]} positioned over motion_blocking_no_leaves run tp {BOT} ~ ~ ~')
            rcon('time set 12500')
            note('scene', label='dusk', t=t)
            dusk_at = time.time()
        if 'dusk' in done and 'task' not in done:
            home_walk = next((r for r in reversed(trace.rows) if r.get('event') == 'call_supervision_default_requested'
                              and r.get('choice') == 'go_home' and r['ts'] >= dusk_at), None)
            if (home_walk and time.time() - home_walk['ts'] > 1.5) or time.time() - dusk_at > 90:
                done.add('task')
                send('/goal 走到 x=135, z=150 附近的地面上（离住处大约 18 格），不破坏、不放置方块，不采集。'
                     '到了之后如实报告实际位置并结束这个任务。')
                note('scene', label='task_during_life', home_walk_seq=home_walk['seq'] if home_walk else None)
        if t >= 600 and 'day' not in done:
            done.add('day')
            rcon('time set day')
            note('scene_end', label='dusk')
        if t >= 1000 and 'visitor2' not in done:
            done.add('visitor2')
            visitor_walks_up(trace, 'visitor_second')
        if t >= 1120 and 'visitor2_leave' not in done:
            done.add('visitor2_leave')
            rcon('player Visitor kill')
            note('scene_end', label='visitor_second')
        minute = int(t // 60)
        if ('minute', minute) not in done:
            done.add(('minute', minute))
            body = next((r for r in reversed(trace.rows) if r.get('event') == 'body_progress'), {})
            note('minute', t=round(t, 1), pos=body.get('pos'), health=body.get('health'),
                 deaths=sum(1 for r in trace.rows if r.get('event') == 'reflex_death'))
        time.sleep(0.5)
    after = wall_state()
    rcon('player Visitor kill')
    rcon(f'data get entity {BOT} Health')
    note('done', observed_s=round(time.time() - start, 1), wall_before=before, wall_after=after,
         wall_unchanged=before == after and all(after.values()),
         deaths=sum(1 for r in trace.rows if r.get('event') == 'reflex_death'))


if __name__ == '__main__':
    main()
