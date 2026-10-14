from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/control-step5-next-c.py').read_text()
source = source.replace('reflex-always-on-20261014-step5-next-c', 'reflex-always-on-20261014-step5-next-d')
source = source.replace('[190,121,190]，再到 [250,121,250]，最后到 [220,121,220]', '[250,121,220]，再到 [190,121,220]，最后到 [220,121,220]')
source = source.replace('summon minecraft:skeleton ~ ~ ~8', 'summon minecraft:skeleton ~8 ~ ~')
exec(compile(source, __file__, 'exec'))
