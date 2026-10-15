from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/start-life-fixed-g.py').read_text()
source = source.replace('step7-fixed-g', 'reconnect-check').replace('Step3CurrentBot', 'Step5NextBot')
source = source.replace('for port in (25569, 25579, 8771):', 'for port in (8771,):')
start = source.index("with (RUN / 'server-launch.log').open('wb') as log:")
end = source.index('profile = json.loads(', start)
source = source[:start] + "from types import SimpleNamespace\nserver = SimpleNamespace(pid=2167201)\n" + source[end:]
start = source.index('profile = json.loads(')
end = source.index('config = ', start)
source = source[:start] + source[end:]
source = source.replace(", 'MINEBOT_LIFE_PROFILE': str(profile_path)", '')
source = source.replace("'scope': 'Retained world/player; no initial task; isolated config; natural mobs enabled.'",
    "'scope': 'Same world and bot after controlled disconnect; new client and SDK session; real Agent goal confirms physical work after rejoin.'")
exec(compile(source, __file__, 'exec'))
