"""Reuse the isolated user launcher; retained artificial test world."""
from pathlib import Path
root=Path('/home/qwait/MineBot-always-on')
source=(root/'logs/start-life-fixed-g.py').read_text()
source=source.replace('step7-fixed-g','default-equipment-fixed').replace('Step3CurrentBot','Step5NextBot')
begin=source.index('profile = json.loads(')
end=source.index('config = ',begin)
source=source[:begin]+source[end:]
source=source.replace(", 'MINEBOT_LIFE_PROFILE': str(profile_path)", '')
source=source.replace("'scope': 'Retained world/player; no initial task; isolated config; natural mobs enabled.'",
    "'scope': 'Retained artificial platform world/player; isolated SDK state, no life profile; controlled damaged weapon, spare weapon and NoAI target; real Agent /goal patrol.'")
source=source.replace("'code_dirty': False", "'code_dirty': bool(subprocess.check_output(['git', 'diff', '--name-only'], cwd=ROOT, text=True).strip())")
exec(compile(source,__file__,'exec'))
