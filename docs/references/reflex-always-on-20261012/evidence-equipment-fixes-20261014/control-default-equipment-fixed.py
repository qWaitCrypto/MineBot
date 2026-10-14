"""Actual Agent patrol interrupted by a controlled near hostile and weapon break."""
from pathlib import Path
root=Path('/home/qwait/MineBot-always-on')
helpers=(root/'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers=helpers.replace('reflex-always-on-20261014-step5-next','reflex-always-on-20261014-default-equipment-fixed')
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
task=send('/goal 请用 delegate_work 交出一段巡视：走到[250,121,250]，再回到[220,121,220]。只走路，不破坏、不放置、不采集。意图写实际要到达的位置。手上的工具和附近威胁由反射层自行处理，不要主动换装备，不读read_reflex，不轮询进度；下发后做后续规划，收到主动通知再如实报告。')
try:
    work=wait(lambda r:r['event']=='call_supervision_request' and r.get('purpose')=='running_frame' and r.get('tool')=='move_to',
        'agent_patrol_running',seconds=100,after=task['last_seq'])
    if work:
        command('fixture_close_target',f'execute at {BOT} run summon minecraft:zombie ~1 ~ ~ {{NoAI:1b,PersistenceRequired:1b,Tags:["equipment_fixed"]}}')
        chosen=wait(lambda r:r['event']=='call_supervision_result' and str(r.get('choice','')).startswith('equip:'),
            'equipment_selected',seconds=40,after=work['seq'])
        if chosen:
            wait(lambda r:r['event']=='call_supervision_finished' and r.get('tool')=='equip_item',
                'equipment_call_finished',seconds=25,after=chosen['seq'])
        wait(lambda r:r['event']=='reflex_activity_finished','patrol_terminal',seconds=65,after=work['seq'])
    command('fixture_final_inventory',f'data get entity {BOT} Inventory')
    command('fixture_final_selected_slot',f'data get entity {BOT} SelectedItemSlot')
    command('fixture_final_target_health','data get entity @e[tag=equipment_fixed,limit=1] Health')
    command('fixture_final_health',f'data get entity {BOT} Health')
    command('fixture_final_position',f'data get entity {BOT} Pos')
    mark('fixture_complete',deaths=[r['seq'] for r in rows() if r['event']=='reflex_death'])
finally:
    command('fixture_target_cleanup','kill @e[tag=equipment_fixed]')
    send('/quit')
