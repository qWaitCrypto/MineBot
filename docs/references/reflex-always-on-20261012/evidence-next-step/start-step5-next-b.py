"""Reuse the isolated launcher, retaining only this fixture's server/world."""
import json
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/start-life-fixed-g.py').read_text()
source = source.replace('step7-fixed-g', 'step5-next-b').replace('Step3CurrentBot', 'Step5NextBot')
source = source.replace('for port in (25569, 25579, 8771):', 'for port in (8771,):')
begin = source.index("with (RUN / 'server-launch.log').open('wb') as log:")
end = source.index('profile = json.loads(', begin)
owned = json.loads((root / 'logs/reflex-always-on-20261014-step5-next/acceptance-run.json').read_text())
source = source[:begin] + 'from types import SimpleNamespace\nserver = SimpleNamespace(pid=' + str(owned['server_pid']) + ')\n' + source[end:]
begin = source.index('profile = json.loads(')
end = source.index('config = Path(', begin)
source = source[:begin] + source[end:]
source = source.replace("'MINEBOT_CLIENT_FREE_CURSOR': 'true', 'MINEBOT_LIFE_PROFILE': str(profile_path),", "'MINEBOT_CLIENT_FREE_CURSOR': 'true',")
source = source.replace("'scope': 'Retained world/player; no initial task; isolated config; natural mobs enabled.'",
    "'scope': 'Retained artificial Step5 arena/world/player; new SDK state, no life profile. Fixture equips sword and applies damage. Correct namespaced gamerules, no daylight burning.'")
exec(compile(source, __file__, 'exec'))
