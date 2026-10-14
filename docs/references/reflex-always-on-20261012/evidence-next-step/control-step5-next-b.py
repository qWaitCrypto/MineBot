from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/control-step5-next.py').read_text()
source = source.replace('reflex-always-on-20261014-step5-next', 'reflex-always-on-20261014-step5-next-b')
source = source.replace('gamerule spawnMobs false', 'gamerule minecraft:spawn_mobs false')
source = source.replace('gamerule advanceTime false', 'gamerule minecraft:advance_time false')
source = source.replace('time set day', 'time set night')
# Saturation permits fast regeneration and hid the desired residual injury.
# Feed to full, then reset it below the regeneration level before damage.
source = source.replace("command('fixture_empty_hand', f'clear {BOT}')",
    "command('fixture_empty_hand', f'clear {BOT}')\n        command('fixture_no_regeneration', 'gamerule minecraft:natural_health_regeneration false')")
exec(compile(source, __file__, 'exec'))
