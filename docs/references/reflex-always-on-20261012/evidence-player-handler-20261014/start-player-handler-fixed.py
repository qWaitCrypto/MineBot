"""New isolated SDK session; retained test platform and empty Agent state."""
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/start-life-fixed-g.py').read_text()
source = source.replace('step7-fixed-g', 'player-handler-fixed').replace('Step3CurrentBot', 'Step5NextBot')
source = source.replace('step7-current-f/life-profile.json', 'step7-next/life-profile.json')
source = source.replace("'code_dirty': False", "'code_dirty': bool(subprocess.check_output(['git', 'diff', '--name-only'], cwd=ROOT, text=True).strip())")
source = source.replace("'scope': 'Retained world/player; no initial task; isolated config; natural mobs enabled.'",
    "'scope': 'Short regression after player alias and handler continuation fixes; retained artificial platform, profile, fresh SDK state; controlled Visitor and real Agent patrol. Not thirty-minute acceptance.'")
exec(compile(source, __file__, 'exec'))
