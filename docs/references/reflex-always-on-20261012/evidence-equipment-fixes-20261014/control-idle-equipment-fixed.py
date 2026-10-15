"""Real /goal first; weapon-break threat after patrol is terminal, while idle."""
from pathlib import Path
root=Path('/home/qwait/MineBot-always-on')
helpers=(root/'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers=helpers.replace('reflex-always-on-20261014-step5-next','reflex-always-on-20261014-idle-equipment-fixed')
exec(compile(helpers,__file__,'exec'))
wait(lambda r:r['event']=='call_supervision_request','first_question',seconds=240)
command('fixture_no_natural_spawns','gamerule minecraft:spawn_mobs false')
command('fixture_night','time set night')
command('fixture_frozen_time','gamerule minecraft:advance_time false')
command('fixture_start',f'tp {BOT} 220.5 121 220.5')
command('fixture_heal',f'effect give {BOT} minecraft:instant_health 1 10 true')
command('fixture_empty_inventory',f'clear {BOT}')
command('fixture_last_durability',f'item replace entity {BOT} weapon.mainhand with minecraft:wooden_sword[minecraft:damage=58]')
command('fixture_spare',f'give {BOT} minecraft:stone_sword')
task=send('/goal 请用 delegate_work 交出一个移动节点，走到[224,121,220]。意图写到达该位置，不破坏、不放置、不采集。下发后不read_reflex、不轮询；收到完成通知后如实报告并待命。不要主动换装备，之后的附近威胁由反射层处理。')
try:
    terminal=wait(lambda r:r['event']=='reflex_activity_finished','agent_patrol_terminal',seconds=100,after=task['last_seq'])
    if terminal:
        idle=wait(lambda r:r['event']=='call_supervision_request' and r.get('purpose')=='idle_frame',
            'idle_after_patrol',seconds=20,after=terminal['seq'])
        if idle:
            command('fixture_close_target',f'execute at {BOT} run summon minecraft:zombie ~1 ~ ~ {{NoAI:1b,PersistenceRequired:1b,Tags:["idle_equipment_fixed"]}}')
            chosen=wait(lambda r:r['event']=='call_supervision_result' and str(r.get('choice','')).startswith('equip:'),
                'equipment_selected',seconds=40,after=idle['seq'])
            if chosen:
                wait(lambda r:r['event']=='tool_raw_result' and r.get('tool')=='engage_entity' and (r.get('raw_result') or {}).get('reason')=='killed',
                    'target_killed_by_body',seconds=35,after=chosen['seq'])
                wait(lambda r:r['event']=='call_supervision_default_finished' and r.get('tool')=='engage_entity',
                    'idle_protection_finished',seconds=15,after=chosen['seq'])
    command('fixture_final_inventory',f'data get entity {BOT} Inventory')
    command('fixture_final_selected_slot',f'data get entity {BOT} SelectedItemSlot')
    command('fixture_final_target_health','data get entity @e[tag=idle_equipment_fixed,limit=1] Health')
    command('fixture_final_health',f'data get entity {BOT} Health')
    command('fixture_final_position',f'data get entity {BOT} Pos')
    mark('fixture_complete',deaths=[r['seq'] for r in rows() if r['event']=='reflex_death'])
finally:
    command('fixture_target_cleanup','kill @e[tag=idle_equipment_fixed]')
    send('/quit')
