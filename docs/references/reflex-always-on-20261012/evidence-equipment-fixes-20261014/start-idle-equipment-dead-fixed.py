from pathlib import Path
root=Path('/home/qwait/MineBot-always-on')
source=(root/'logs/start-default-equipment-fixed.py').read_text().replace("source=source.replace('step7-fixed-g','default-equipment-fixed')", "source=source.replace('step7-fixed-g','idle-equipment-dead-fixed')")
exec(compile(source,__file__,'exec'))
