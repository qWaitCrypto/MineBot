from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/start-step5-next-b.py').read_text()
source = source.replace("'step5-next-b'", "'step6-backup-next'")
exec(compile(source, __file__, 'exec'))
