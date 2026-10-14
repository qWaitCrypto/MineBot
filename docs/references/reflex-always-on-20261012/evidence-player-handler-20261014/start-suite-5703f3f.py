"""Full suite for the deployed fix, isolated env, source remains frozen."""
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/start-suite-0a2840b.py').read_text()
source = source.replace('0a2840b', '5703f3f').replace("'concurrent_game_run':'life-30-final'", "'concurrent_game_run':'player-handler-fixed'")
exec(compile(source, __file__, 'exec'))
