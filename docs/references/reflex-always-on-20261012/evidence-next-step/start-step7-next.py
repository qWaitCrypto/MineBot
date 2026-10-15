"""Current committed source, retained artificial arena, a fresh SDK life session."""
import json
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/start-life-fixed-g.py').read_text()
source = source.replace('step7-fixed-g', 'step7-next').replace('Step3CurrentBot', 'Step5NextBot')
source = source.replace('for port in (25569, 25579, 8771):', 'for port in (8771,):')
begin = source.index("with (RUN / 'server-launch.log').open('wb') as log:")
end = source.index('profile = json.loads(', begin)
owned = json.loads((root / 'logs/reflex-always-on-20261014-step5-next/acceptance-run.json').read_text())
source = source[:begin] + 'from types import SimpleNamespace\nserver = SimpleNamespace(pid=' + str(owned['server_pid']) + ')\n' + source[end:]
begin = source.index('profile = json.loads(')
end = source.index('profile_path = ', begin)
source = source[:begin] + "profile = {'world': props['level-name'], 'dimension': 'overworld', 'home': {'pos': [220.5,121,220.5], 'note': '人工测试高台中心'}, 'routine': '白天在住处附近待着，有人来就过去看看；天黑前回住处。', 'relationships': [{'player': 'Visitor', 'note': '他在附近时我会过去看看他在做什么。'}], 'personality': '安静，好奇，不爱冒险。'}\n" + source[end:]
source = source.replace("'scope': 'Retained world/player; no initial task; isolated config; natural mobs enabled.'",
    "'scope': 'Retained artificial arena and player, new SDK state, hand-authored life profile. Natural spawning disabled; Visitor/time/teleport are controlled fixtures. Mid-run task uses /goal.'")
exec(compile(source, __file__, 'exec'))
