from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
helpers = (root / 'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers = helpers.replace('reflex-always-on-20261014-step5-next', 'reflex-always-on-20261014-interrupt-instruction-fixed')
exec(compile(helpers, __file__, 'exec'))
try:
    if wait(lambda r:r['event']=='call_supervision_request', 'first_idle', seconds=240) is None:
        raise RuntimeError('No reflex session')
    command('fixture_start', f'tp {BOT} 220.5 121 220.5')
    command('fixture_empty_inventory', f'clear {BOT}')
    command('fixture_held_weapon', f'item replace entity {BOT} weapon.mainhand with minecraft:wooden_sword')
    command('fixture_spare_weapon', f'give {BOT} minecraft:stone_sword')
    task = send('/goal 请把高台巡视交给delegate_work：到[250,121,220]，再到[190,121,220]，最后回到[220,121,220]。plan最外层intent写走完三站并回起点，每个移动intent写到达该点。加interrupt_when：当主手为空并且背包里有石剑，handler装备背包石剑到主手，之后继续巡视。条件原话和handler意图由你写进树，不在Agent回合自己轮询或直接补装备。不破坏、不放置、不采集。下发后想后面的事，等完成通知再报告。')
    running = wait(lambda r:r['event']=='call_supervision_request' and r.get('purpose')=='running_frame'
        and 'interrupt' in r.get('request', {}).get('questions', {}).get('q', {}).get('criteria', {}),
        'real_agent_interrupt_tree_running', seconds=100, after=task['last_seq'])
    if running:
        boundary = mark('empty_hand_trigger')
        command('fixture_remove_held_weapon', f'item replace entity {BOT} weapon.mainhand with minecraft:air')
        selected = wait(lambda r:r['event']=='call_supervision_result' and r.get('choice')=='interrupt',
                        'decision_selected_interrupt', seconds=35, after=boundary['last_seq'])
        if selected:
            wait(lambda r:r['event']=='tool_raw_result' and r.get('tool')=='equip_item' and (r.get('raw_result') or {}).get('success'),
                 'handler_equipped', seconds=35, after=selected['seq'])
            wait(lambda r:r['event']=='call_supervision_result' and r.get('choice')=='resume',
                 'decision_resumed_work', seconds=35, after=selected['seq'])
        terminal = wait(lambda r:r['event']=='reflex_activity_finished', 'patrol_terminal', seconds=100, after=task['last_seq'])
        if terminal:
            wait(lambda r:r['event']=='reflex_fact_wake', 'agent_received_terminal', seconds=60, after=terminal['seq'])
    command('final_inventory', f'data get entity {BOT} Inventory')
    command('final_selected', f'data get entity {BOT} SelectedItemSlot')
    command('final_position', f'data get entity {BOT} Pos')
    command('final_health', f'data get entity {BOT} Health')
    mark('interrupt_test_complete', deaths=[r['seq'] for r in rows() if r['event']=='reflex_death'])
finally:
    send('/quit')
