from pathlib import Path
root=Path('/home/qwait/MineBot-always-on')
source=(root/'logs/control-idle-equipment-fixed.py').read_text().replace('reflex-always-on-20261014-idle-equipment-fixed','reflex-always-on-20261014-idle-equipment-context-fixed')
exec(compile(source,__file__,'exec'))
