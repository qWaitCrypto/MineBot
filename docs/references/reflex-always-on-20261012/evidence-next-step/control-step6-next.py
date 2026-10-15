"""Real Agent authors interrupt_when; fixture changes the held item during work."""
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
helpers = (root / 'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers = helpers.replace('reflex-always-on-20261014-step5-next', 'reflex-always-on-20261014-step6-next')
exec(compile(helpers, __file__, 'exec'))

wait(lambda r: r['event'] == 'call_supervision_request', 'first_question', seconds=240)
command('fixture_position', f'tp {BOT} 220.5 121 220.5')
command('fixture_heal', f'effect give {BOT} minecraft:instant_health 1 10 true')
command('fixture_empty_inventory', f'clear {BOT}')
command('fixture_main_hand', f'item replace entity {BOT} weapon.mainhand with minecraft:wooden_sword')
command('fixture_spare', f'item replace entity {BOT} hotbar.1 with minecraft:stone_sword')
time.sleep(1.2)
task = send('这是计划中断验收。巡视高台：先到 [250,121,220]，再到 [190,121,220]，最后回 [220,121,220]。用 delegate_work 写计划，整体和各步写意图。计划的 interrupt_when 写这个条件：主手已经空了，而且背包里有石剑；handler 为装备背包里的石剑，之后继续原来的巡视。现在主手是木剑，夹具会在巡视期间把它移除。不要在 Agent 主回合轮询或主动换装备，不采集、不建造、不破坏方块。收到主动通知后如实报告。')
work = wait(lambda r: r['event'] == 'call_supervision_request' and r.get('loop_state') == 'work'
            and r.get('purpose') == 'running_frame', 'real_agent_work', seconds=100, after=task['last_seq'])
if work:
    command('fixture_remove_held_item_during_work', f'item replace entity {BOT} weapon.mainhand with minecraft:air')
    changed = mark('condition_became_true')
    chosen = wait(lambda r: r['event'] == 'call_supervision_result' and r.get('choice') == 'interrupt',
                  'interrupt_selected', seconds=25, after=changed['last_seq'])
    if chosen:
        wait(lambda r: r['event'] == 'call_supervision_request' and r.get('tool') == 'equip_item',
             'handler_equip_requested', seconds=20, after=chosen['seq'])
        wait(lambda r: r['event'] == 'call_supervision_result' and r.get('choice') == 'resume',
             'handler_resume_selected', seconds=20, after=chosen['seq'])
    terminal = wait(lambda r: r['event'] == 'reflex_activity_finished', 'plan_terminal', seconds=65, after=work['seq'])
    # Keep the session open long enough for the queued terminal to reach Agent.
    wait(lambda r: r['event'] == 'reflex_fact_wake', 'agent_terminal_wake', seconds=12,
         after=terminal['seq'] if terminal else work['seq'])
    command('fixture_final_inventory', f'data get entity {BOT} Inventory')
    command('fixture_final_selected_slot', f'data get entity {BOT} SelectedItemSlot')
    command('fixture_final_health', f'data get entity {BOT} Health')
mark('fixture_complete', deaths=[r['seq'] for r in rows() if r['event'] == 'reflex_death'])
send('/quit')
