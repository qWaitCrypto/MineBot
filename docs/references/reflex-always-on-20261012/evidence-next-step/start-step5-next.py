"""Reuse the owned real-client launcher for a new artificial threat fixture."""
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/step3-current-live.py').read_text()
source = source.replace('reflex-always-on-20261014-step3-current', 'reflex-always-on-20261014-step5-next')
source = source.replace('Step3CurrentBot', 'Step5NextBot')
source = source.replace('server.properties.before-step3-current-6db65ec', 'server.properties.before-step5-next-edc89ff')
source = source.replace('step3-current-6db65ec-20261014', 'step5-next-edc89ff-20261014')
source = source.replace("    goal = '从空背包开始自主搜集材料，制作并装备一把石镐，然后采集至少4份生铁。自己决定身体工具、调用顺序和工作计划；不要传送，不要外部补给。最后如实报告实际结果。'",
    "    import tempfile\n    config = Path(tempfile.mkdtemp(prefix='minebot-step5-next-config-'))\n    (config / 'minebot').mkdir()\n    (config / 'minebot/servers.toml').write_text('')\n    environment['XDG_CONFIG_HOME'] = str(config)\n    goal = None")
source = source.replace("'--run-dir', str(RUN), '--plain', '--foreground', goal]", "'--run-dir', str(RUN), '--plain', '--foreground']")
source = source.replace("'started_at': time.time(), 'model':", "'scope': 'New world/player, artificial platform/mobs/sword/damage; no initial Agent goal. Runtime threat evidence first, then real Agent patrol.', 'started_at': time.time(), 'model':")
source = source[:source.index('\ndef status():')] + '\nstart()\n'
exec(compile(source, __file__, 'exec'))
