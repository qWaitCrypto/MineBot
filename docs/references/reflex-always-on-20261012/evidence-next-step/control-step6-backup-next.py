"""Authorised durability fixture; the real Agent chooses and submits its call."""
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
helpers = (root / 'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers = helpers.replace('reflex-always-on-20261014-step5-next', 'reflex-always-on-20261014-step6-backup-next')
exec(compile(helpers, __file__, 'exec'))

wait(lambda r: r['event'] == 'call_supervision_request', 'first_question', seconds=240)
command('fixture_position', f'tp {BOT} 220.5 121 220.5')
command('fixture_heal', f'effect give {BOT} minecraft:instant_health 1 10 true')
command('fixture_empty_inventory', f'clear {BOT}')
command('fixture_last_durability', f'item replace entity {BOT} weapon.mainhand with minecraft:wooden_sword[minecraft:damage=58]')
command('fixture_spare', f'give {BOT} minecraft:stone_sword')
command('fixture_far_target', 'summon minecraft:zombie 242.5 121 220.5 {NoAI:1b,PersistenceRequired:1b,Tags:["step6_next_backup"]}')
time.sleep(1.2)
task = send('这是已有备用工具验收。夹具给了你一把剩一次耐久的木剑和备用石剑，在东边约22格放了不移动的僵尸。只读一次附近实体取它的UUID，用 delegate_work 交出单节点 engage_entity，意图是击杀该僵尸。不要在Agent回合主动equip_item；工具损坏后让反射层自己决定是否换备用并继续同一目标。不read_reflex、不轮询、不采集、不建造、不破坏方块。收到主动通知后如实报告。')
work = wait(lambda r: r['event'] == 'work_call' and r.get('tool') == 'engage_entity',
            'agent_combat_node_started', seconds=100, after=task['last_seq'])
if work:
    command('fixture_target_into_reach', f'execute at {BOT} run tp @e[tag=step6_next_backup,limit=1] ~1 ~ ~')
    chosen = wait(lambda r: r['event'] == 'call_supervision_result' and str(r.get('choice', '')).startswith('equip:'),
                  'backup_equipment_selected', seconds=30, after=work['seq'])
    wait(lambda r: r['event'] == 'reflex_activity_finished', 'plan_terminal', seconds=35, after=work['seq'])
    command('fixture_final_inventory', f'data get entity {BOT} Inventory')
    command('fixture_final_selected_slot', f'data get entity {BOT} SelectedItemSlot')
    command('fixture_final_target_health', 'data get entity @e[tag=step6_next_backup,limit=1] Health')
    command('fixture_final_health', f'data get entity {BOT} Health')
command('fixture_target_cleanup', 'kill @e[tag=step6_next_backup]')
mark('fixture_complete', deaths=[r['seq'] for r in rows() if r['event'] == 'reflex_death'])
send('/quit')
