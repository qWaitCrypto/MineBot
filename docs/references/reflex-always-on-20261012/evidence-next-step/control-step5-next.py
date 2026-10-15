"""Artificial threat validation: record interventions, do not select answers."""
import importlib.util
import json
import time
from pathlib import Path

ROOT = Path('/home/qwait/MineBot-always-on')
RUN = ROOT / 'logs/reflex-always-on-20261014-step5-next'
BOT = 'Step5NextBot'
spec = importlib.util.spec_from_file_location('existing_fixture', ROOT / 'logs/step3-current-live.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

def rows():
    path = RUN / 'trace.jsonl'
    result = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                pass
    return result

def mark(kind, **facts):
    data = rows()
    event = {'time': time.time(), 'last_seq': data[-1]['seq'] if data else None, 'kind': kind, **facts}
    with (RUN / 'fixture-events.jsonl').open('a') as out:
        out.write(json.dumps(event, ensure_ascii=False) + '\n')
    print(json.dumps(event, ensure_ascii=False), flush=True)
    return event

def command(kind, text):
    response = fixture.rcon(text)
    mark(kind, command=text, response=response)
    return response

def wait(predicate, label, seconds=45, after=0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        data = rows()
        match = next((r for r in reversed(data) if r.get('seq', 0) > after and predicate(r)), None)
        if match:
            mark(label, matched_seq=match['seq'])
            return match
        if (RUN / 'launcher-returncode.json').exists():
            raise RuntimeError('Launcher ended before ' + label)
        time.sleep(.25)
    mark('window_unavailable', label=label)
    return None

def send(text):
    with (RUN / 'control-stdin.txt').open('a') as out:
        out.write(text + '\n')
    return mark('user_message', text=text)

wait(lambda r: r['event'] == 'call_supervision_request', 'first_question', seconds=240)
command('fixture_no_natural_spawns', 'gamerule spawnMobs false')
command('fixture_day', 'time set day')
command('fixture_day_frozen', 'gamerule advanceTime false')
command('fixture_difficulty', 'difficulty easy')
command('fixture_platform', 'fill 180 120 180 260 120 260 minecraft:stone')
command('fixture_position', f'tp {BOT} 220.5 121 220.5')
command('fixture_heal', f'effect give {BOT} minecraft:instant_health 1 10 true')
command('fixture_feed', f'effect give {BOT} minecraft:saturation 1 10 true')
time.sleep(1.5)
command('fixture_clear_effects', f'effect clear {BOT}')
command('fixture_empty_inventory', f'clear {BOT}')
command('fixture_sword', f'item replace entity {BOT} weapon.mainhand with minecraft:iron_sword')
time.sleep(2)
begin = mark('melee_begin')
command('fixture_melee', f'execute at {BOT} run summon minecraft:zombie ~1 ~ ~ {{PersistenceRequired:1b,Tags:["step5_next_melee"]}}')
chosen = wait(lambda r: r['event'] == 'call_supervision_default_requested' and r.get('choice') == 'engage',
              'melee_engage_selected', seconds=25, after=begin['last_seq'])
settled = wait(lambda r: r['event'] == 'call_supervision_default_finished' and r.get('tool') == 'engage_entity',
               'melee_action_ended', seconds=30, after=chosen['seq'] if chosen else begin['last_seq'])
command('fixture_melee_health', f'data get entity {BOT} Health')
command('fixture_melee_target_health', 'data get entity @e[tag=step5_next_melee,limit=1] Health')
command('fixture_melee_cleanup', 'kill @e[tag=step5_next_melee]')
command('fixture_heal', f'effect give {BOT} minecraft:instant_health 1 10 true')
command('fixture_feed', f'effect give {BOT} minecraft:saturation 1 10 true')
time.sleep(1.2)
command('fixture_clear_effects', f'effect clear {BOT}')

task = send('这是人工威胁验收。现在在高台巡视：依次到 [250,121,220]、[250,121,250]、[190,121,250]、[190,121,190]、[220,121,220]。把路线交给 delegate_work，各步意图写到达目标位置。临时威胁交给反射层，不主动调用战斗工具，不轮询进度；完成后待命。不采集、不建造、不破坏方块。')
work = wait(lambda r: r['event'] == 'call_supervision_request' and r.get('loop_state') == 'work'
            and r.get('purpose') == 'running_frame', 'real_agent_work', seconds=100, after=task['last_seq'])
if work:
    far = mark('far_control_begin')
    command('fixture_far_zombie', f'execute at {BOT} run summon minecraft:zombie ~ ~ ~20 {{NoAI:1b,PersistenceRequired:1b,Tags:["step5_next_far"]}}')
    time.sleep(3)
    command('fixture_far_cleanup', 'kill @e[tag=step5_next_far]')
    mark('far_control_end')
    active = wait(lambda r: r['event'] == 'call_supervision_request' and r.get('loop_state') == 'work'
                  and r.get('purpose') == 'running_frame', 'work_for_ranged', seconds=20, after=rows()[-1]['seq'])
    if active:
        command('fixture_empty_hand', f'clear {BOT}')
        command('fixture_ranged_damage', f'damage {BOT} 14 minecraft:generic')
        begin = mark('ranged_begin')
        command('fixture_ranged_skeleton', f'execute at {BOT} run summon minecraft:skeleton ~ ~ ~8 {{PersistenceRequired:1b,Tags:["step5_next_ranged"]}}')
        escape = wait(lambda r: r['event'] == 'call_supervision_default_requested' and r.get('choice') in {'move_away', 'retreat_safe'},
                      'ranged_escape_selected', seconds=20, after=begin['last_seq'])
        time.sleep(2)
        command('fixture_ranged_remove_threat', 'kill @e[tag=step5_next_ranged]')
        mark('threat_removed')
        if escape:
            ended = wait(lambda r: r['event'] == 'call_supervision_default_finished', 'ranged_action_ended',
                         seconds=35, after=escape['seq'])
            resumed = wait(lambda r: r['event'] == 'call_supervision_result' and r.get('choice') == 'resume',
                           'resume_selected', seconds=25, after=ended['seq'] if ended else escape['seq'])
            wait(lambda r: r['event'] == 'call_supervision_request' and r.get('loop_state') == 'work'
                 and r.get('purpose') == 'running_frame', 'original_work_resumed', seconds=25,
                 after=resumed['seq'] if resumed else escape['seq'])
        command('fixture_final_health', f'data get entity {BOT} Health')
        command('fixture_heal_after_measurement', f'effect give {BOT} minecraft:instant_health 1 10 true')

mark('fixture_complete', deaths=[r['seq'] for r in rows() if r['event'] == 'reflex_death'])
send('/quit')
