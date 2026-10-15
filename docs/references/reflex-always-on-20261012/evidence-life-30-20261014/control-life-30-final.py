"""Thirty minutes with real Decision; stop on authoritative disconnect/exit."""
import os
import time
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/control-step7-next.py').read_text()
source = source.replace('reflex-always-on-20261014-step7-next', 'reflex-always-on-20261014-life-30-final-v2')
source = source.replace("command('fixture_no_new_natural_spawns', 'gamerule minecraft:spawn_mobs false')",
    "command('fixture_natural_spawns', 'gamerule minecraft:spawn_mobs true')")
source = source.replace("command('fixture_empty_inventory', f'clear {BOT}')", "command('fixture_retained_inventory', f'data get entity {BOT} Inventory')")
source = source.replace("command('fixture_start_away_from_home',", "command('fixture_remove_old_visitor', 'player Visitor kill')\ncommand('fixture_start_away_from_home',")
source = source.replace("    data = rows()\n    body =", """    data = rows()
    metadata = json.loads((RUN / 'acceptance-run.json').read_text())
    os.kill(metadata['launcher_pid'], 0)
    if (RUN / 'launcher-returncode.json').exists():
        mark('observation_ended_early', reason='launcher_exited', observed_s=round(elapsed,1))
        break
    disconnected = next((r for r in reversed(data) if r['event']=='reflex_loop_stopped'
        and r.get('reason')=='server_disconnected'), None)
    if disconnected:
        mark('observation_ended_early', reason='native_disconnect', stopped_seq=disconnected['seq'], observed_s=round(elapsed,1))
        break
    body =""")
source = source.replace("        mark('observation_minute',", "        command('minute_health', f'data get entity {BOT} Health')\n        mark('observation_minute',")
source = source.replace("send('/quit')", "command('fixture_visitor_cleanup', 'player Visitor kill')\nsend('/quit')")
exec(compile(source, __file__, 'exec'))
