"""Remove the ranged threat while escape is RUNNING, before its result question."""
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
helpers = (root / 'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers = helpers.replace('reflex-always-on-20261014-step5-next', 'reflex-always-on-20261014-step5-next-c')
exec(compile(helpers, __file__, 'exec'))

wait(lambda r: r['event'] == 'call_supervision_request', 'first_question', seconds=240)
command('fixture_position', f'tp {BOT} 220.5 121 220.5')
command('fixture_heal', f'effect give {BOT} minecraft:instant_health 1 10 true')
command('fixture_empty_inventory', f'clear {BOT}')
command('fixture_no_regeneration', 'gamerule minecraft:natural_health_regeneration false')
time.sleep(1.2)
task = send('这是人工威胁恢复验收。把巡视路线交给 delegate_work：先到 [190,121,190]，再到 [250,121,250]，最后到 [220,121,220]。各步意图写到达目标位置。不采集、不建造、不破坏方块。临时威胁让反射层自行处理，不主动调用战斗工具，不轮询。完成后如实报告。')
work = wait(lambda r: r['event'] == 'call_supervision_request' and r.get('loop_state') == 'work'
            and r.get('purpose') == 'running_frame', 'real_agent_work', seconds=100, after=task['last_seq'])
if work:
    command('fixture_damage', f'damage {BOT} 14 minecraft:generic')
    begin = mark('ranged_begin')
    command('fixture_ranged_skeleton', f'execute at {BOT} run summon minecraft:skeleton ~ ~ ~8 {{PersistenceRequired:1b,Tags:["step5_next_resume"]}}')
    escape = wait(lambda r: r['event'] == 'call_supervision_default_requested' and r.get('choice') in {'move_away', 'retreat_safe'},
                  'escape_selected', seconds=20, after=begin['last_seq'])
    if escape:
        wait(lambda r: r['event'] == 'reflex_reaction_chain' and r.get('choice') in {'move_away', 'retreat_safe'},
             'escape_native_progress', seconds=5, after=escape['seq'])
    command('fixture_remove_while_escape_running', 'kill @e[tag=step5_next_resume]')
    removed = mark('threat_removed')
    if escape:
        resume = wait(lambda r: r['event'] == 'call_supervision_result' and r.get('choice') == 'resume',
                      'resume_after_threat_removed', seconds=25, after=removed['last_seq'])
        if resume:
            wait(lambda r: r['event'] == 'call_supervision_request' and r.get('loop_state') == 'work'
                 and r.get('purpose') == 'running_frame', 'original_work_resumed', seconds=25, after=resume['seq'])
    completion = wait(lambda r: r['event'] == 'reflex_activity_finished',
                      'plan_terminal', seconds=65, after=work['seq'])
    command('fixture_final_health', f'data get entity {BOT} Health')
    command('fixture_final_position', f'data get entity {BOT} Pos')
mark('fixture_complete', deaths=[r['seq'] for r in rows() if r['event'] == 'reflex_death'])
send('/quit')
