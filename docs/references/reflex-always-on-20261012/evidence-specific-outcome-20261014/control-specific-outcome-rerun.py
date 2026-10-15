from pathlib import Path
root=Path('/home/qwait/MineBot-always-on')
source=(root/'logs/control-player-handler-fixed.py').read_text().replace('player-handler-fixed','specific-outcome-rerun').replace('seconds=240','seconds=600')
exec(compile(source,__file__,'exec'))
