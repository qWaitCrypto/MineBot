from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/control-disconnect-check.py').read_text().replace('disconnect-check', 'disconnect-fixed')
exec(compile(source, __file__, 'exec'))
