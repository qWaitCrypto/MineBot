from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
helpers = (root / 'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers = helpers.replace('reflex-always-on-20261014-step5-next', 'reflex-always-on-20261014-reconnect-check')
exec(compile(helpers, __file__, 'exec'))
try:
    first = wait(lambda r: r['event'] == 'call_supervision_request', 'first_rejoined_idle', seconds=240)
    if first is None:
        raise RuntimeError('Rejoin has no reflex frames')
    command('rejoined_health', f'data get entity {BOT} Health')
    command('rejoined_position_before', f'data get entity {BOT} Pos')
    boundary = send('/goal 请用 delegate_work 下发一个移动节点到[220,121,220]，intent写到达该位置。不破坏、不放置，不轮询；收到完成事实后报告并待命。')
    wait(lambda r: r['event'] == 'reflex_activity_finished', 'rejoined_goal_terminal', seconds=100, after=boundary['last_seq'])
    command('rejoined_position_after', f'data get entity {BOT} Pos')
    command('rejoined_final_health', f'data get entity {BOT} Health')
    mark('rejoin_complete', deaths=[r['seq'] for r in rows() if r['event'] == 'reflex_death'])
finally:
    send('/quit')
