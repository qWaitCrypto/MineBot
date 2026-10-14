from pathlib import Path
import time
root = Path('/home/qwait/MineBot-always-on')
helpers = (root / 'logs/control-step5-next.py').read_text().split("wait(lambda r: r['event'] == 'call_supervision_request', 'first_question'")[0]
helpers = helpers.replace('reflex-always-on-20261014-step5-next', 'reflex-always-on-20261014-disconnect-check')
exec(compile(helpers, __file__, 'exec'))
try:
    first = wait(lambda r: r['event'] == 'call_supervision_request', 'first_idle_question', seconds=240)
    if first is None:
        raise RuntimeError('No valid reflex session')
    command('before_disconnect_health', f'data get entity {BOT} Health')
    boundary = mark('disconnect_begin')
    command('server_kick', f'kick {BOT} controlled_disconnect_acceptance')
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if (RUN / 'launcher-returncode.json').exists():
            break
        time.sleep(.25)
    data = [r for r in rows() if r['seq'] > boundary['last_seq']]
    mark('disconnect_observation',
         requests=[r['seq'] for r in data if r['event'] == 'call_supervision_request'],
         stops=[r for r in data if r['event'] == 'reflex_loop_stopped'],
         deaths=[r for r in data if r['event'] == 'reflex_death'],
         lifecycle=[r for r in data if 'recovery' in r['event'] or 'connection' in r['event']])
finally:
    if not (RUN / 'launcher-returncode.json').exists():
        send('/quit')
