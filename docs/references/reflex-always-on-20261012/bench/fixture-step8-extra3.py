"""Extra live checks (artificial, all logged), idle Body with a life profile, no task.

1. no_spare_needed (x3): intact wooden_sword in hand, stone_sword in the bag, a zombie
   beside the Body. Once the reflex starts engage_entity, the zombie is moved 60 blocks
   away, so the fight ends with the target lost and the sword intact. Equipping the
   stone sword to "run the same call again" is the wrong answer here.
2. (ranged: passed in step8-extra-claude1, not repeated)
3. follow: Visitor appears 5 blocks away and keeps walking away for 15 s.
4. seek: Visitor leaves; the profile names him and he is out of view.

Spawned mobs carry a tag and are removed by tag; blocks are not touched.
"""
import json, subprocess, sys, time
from pathlib import Path

RUN = Path(sys.argv[1])
BOT = sys.argv[2]
RCON = '/home/qwait/MineBot/docs/references/reflex-always-on-20261012/live/reflex-rcon.py'
LOG = RUN / 'fixture-events.jsonl'
TRACE = RUN / 'trace.jsonl'


def note(kind, **data):
    with LOG.open('a') as sink:
        sink.write(json.dumps({'ts': time.time(), 'kind': kind, **data}, ensure_ascii=False) + '\n')


def rcon(command):
    out = subprocess.run([sys.executable, RCON, command], capture_output=True, text=True, timeout=20)
    note('rcon', command=command, out=(out.stdout or out.stderr).strip()[:300])
    return out.stdout


class Trace:
    def __init__(self):
        self.offset, self.rows = 0, []

    def poll(self):
        if TRACE.exists():
            with TRACE.open('rb') as source:
                source.seek(self.offset)
                for line in source:
                    if not line.endswith(b'\n'):
                        break
                    self.offset += len(line)
                    try:
                        self.rows.append(json.loads(line))
                    except Exception:
                        pass
        return self.rows


def position(trace):
    for row in reversed(trace.poll()):
        if row.get('event') == 'body_progress' and row.get('pos'):
            return row['pos']
    return None


def wait_for(trace, predicate, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        hit = next((r for r in reversed(trace.poll()) if predicate(r)), None)
        if hit:
            return hit
        time.sleep(0.2)
    return None


def heal():
    rcon(f'effect give {BOT} minecraft:instant_health 1 10 true')
    time.sleep(1.5)
    rcon(f'effect clear {BOT}')


def no_spare_needed(trace, attempt):
    # Night for this scene only (mob spawning stays off): in daylight the
    # zombies burned before they reached the Body (step8-extra-claude2).
    rcon('time set 18000')
    heal()
    rcon(f'item replace entity {BOT} weapon.mainhand with minecraft:wooden_sword')
    rcon(f'item replace entity {BOT} inventory.0 with minecraft:stone_sword')
    time.sleep(2)
    since = time.time()
    note('scene', label='no_spare_needed', attempt=attempt, body=position(trace))
    rcon(f'execute at {BOT} run summon minecraft:zombie ~2 ~ ~ {{PersistenceRequired:1b,Tags:["s8_zombie"]}}')
    # The fight may be chosen in an idle frame or in a protective result
    # question; the chain event marks the engage action actually starting.
    engaged = wait_for(trace, lambda r: r['ts'] >= since and (
        (r.get('event') == 'call_supervision_default_requested' and r.get('choice') == 'engage')
        or (r.get('event') == 'reflex_reaction_chain' and r.get('choice') == 'engage')), 25)
    if engaged is None:
        note('scene_skipped', label='no_spare_needed', attempt=attempt, reason='no engage chosen within 25 s')
    else:
        time.sleep(1.0)
        rcon('execute as @e[tag=s8_zombie] at @s run tp @s ~60 ~ ~')
        note('target_moved_away', attempt=attempt, engage_seq=engaged['seq'])
    time.sleep(20)
    rcon('kill @e[tag=s8_zombie]')
    rcon('time set day')
    note('scene_end', label='no_spare_needed', attempt=attempt)
    time.sleep(10)


def main():
    trace = Trace()
    note('start', bot=BOT)
    if wait_for(trace, lambda r: r.get('event') == 'call_supervision_request', 600) is None:
        note('abort', reason='no question within 600 s')
        return
    for command in ('time set day', 'gamerule minecraft:advance_time false', 'gamerule minecraft:spawn_mobs false',
                    'gamerule minecraft:natural_health_regeneration true', 'kill @e[type=minecraft:zombie]',
                    'kill @e[type=minecraft:skeleton]'):
        rcon(command)
    time.sleep(30)
    for attempt in range(3):
        no_spare_needed(trace, attempt)

    heal()
    pos = position(trace)
    note('scene', label='follow', body=pos)
    rcon('player Visitor kill')
    rcon(f'execute at {BOT} run player Visitor spawn at ~5 ~ ~')
    time.sleep(8)
    rcon(f'execute as Visitor at @s facing entity {BOT} feet run tp @s ~ ~ ~ ~180 0')
    rcon('player Visitor move forward')
    time.sleep(15)
    rcon('player Visitor stop')
    time.sleep(20)
    note('scene_end', label='follow')

    rcon('player Visitor kill')
    note('scene', label='seek', body=position(trace))
    time.sleep(120)
    note('scene_end', label='seek')
    note('done')


if __name__ == '__main__':
    main()
