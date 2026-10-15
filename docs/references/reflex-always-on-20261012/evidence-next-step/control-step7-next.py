"""Thirty minutes of real life-loop observation; no fake Decision answers."""
import math
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
helpers = (root / 'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers = helpers.replace('reflex-always-on-20261014-step5-next', 'reflex-always-on-20261014-step7-next')
exec(compile(helpers, __file__, 'exec'))

_cached_rows = []
_trace_offset = 0
def rows():
    global _trace_offset
    path = RUN / 'trace.jsonl'
    if path.exists():
        with path.open() as source:
            source.seek(_trace_offset)
            while True:
                before = source.tell()
                line = source.readline()
                if not line or not line.endswith('\n'):
                    source.seek(before)
                    break
                try:
                    _cached_rows.append(json.loads(line))
                except ValueError:
                    pass
            _trace_offset = source.tell()
    return _cached_rows

wait(lambda r: r['event'] == 'call_supervision_request', 'first_question', seconds=240)
command('fixture_day', 'time set day')
command('fixture_day_frozen', 'gamerule minecraft:advance_time false')
command('fixture_no_new_natural_spawns', 'gamerule minecraft:spawn_mobs false')
command('fixture_restore_regeneration', 'gamerule minecraft:natural_health_regeneration true')
command('fixture_heal', f'effect give {BOT} minecraft:instant_health 1 10 true')
command('fixture_feed', f'effect give {BOT} minecraft:saturation 1 10 true')
command('fixture_empty_inventory', f'clear {BOT}')
command('fixture_start_away_from_home', f'tp {BOT} 250.5 121 250.5')
start = time.monotonic()
begin = mark('life_observation_started', duration_s=1800, home=[220.5,121,220.5])
done = set()
task_seq = None
while time.monotonic() - start < 1800:
    elapsed = time.monotonic() - start
    data = rows()
    body = next((r for r in reversed(data) if r['event'] == 'body_progress' and r.get('pos')), None)
    if elapsed >= 120 and 'visitor' not in done and body:
        x, y, z = body['pos']
        command('fixture_visitor_spawn', f'player Visitor spawn at {x+6:.1f} {y:.1f} {z:.1f}')
        done.add('visitor')
    if elapsed >= 180 and 'walk' not in done:
        command('fixture_visitor_walk', 'player Visitor move forward')
        done.add('walk')
    if elapsed >= 185 and 'stop' not in done:
        command('fixture_visitor_stop', 'player Visitor stop')
        done.add('stop')
    if elapsed >= 230 and 'leave' not in done:
        command('fixture_visitor_leave', 'player Visitor kill')
        done.add('leave')
    if elapsed >= 300 and 'dusk' not in done:
        command('fixture_dusk', 'time set 12500')
        done.add('dusk')
    life = next((r for r in reversed(data) if r['event'] == 'call_supervision_default_requested'
                 and r.get('choice') == 'go_home'), None)
    moving_home = bool(life and body and body.get('pathing') is True
                       and time.time() - body['ts'] < 1.5 and body['ts'] >= life['ts'])
    if 'dusk' in done and 'task' not in done and (moving_home or elapsed >= 360):
        # /goal activates the SDK task lifecycle; ordinary chat does not.
        task = send('/goal 请走到 [210,121,230]，不破坏或放置方块，不采集。意图写到达这个位置；完成后如实报告，然后让身体回到空闲生活。')
        task_seq = task['last_seq']
        mark('task_during_life', while_native_home_move=moving_home,
             native_body_seq=body['seq'] if body else None,
             life_request_seq=life['seq'] if life else None)
        done.add('task')
    if elapsed >= 480 and 'day_again' not in done:
        command('fixture_day_after_task', 'time set day')
        done.add('day_again')
    minute = int(elapsed // 60)
    if minute not in done:
        done.add(minute)
        mark('observation_minute', elapsed_s=round(elapsed, 1), body_seq=body['seq'] if body else None,
             position=body.get('pos') if body else None, health=body.get('health') if body else None,
             deaths=[r['seq'] for r in data if r['event'] == 'reflex_death'])
    time.sleep(.5)
command('fixture_final_health', f'data get entity {BOT} Health')
command('fixture_final_position', f'data get entity {BOT} Pos')
mark('fixture_complete', observed_s=round(time.monotonic()-start, 1),
     deaths=[r['seq'] for r in rows() if r['event'] == 'reflex_death'])
send('/quit')
