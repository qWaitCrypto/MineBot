from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/start-life-fixed-g.py').read_text()
source = source.replace('step7-fixed-g', 'life-30-final').replace('Step3CurrentBot', 'Step5NextBot')
source = source.replace('step7-current-f/life-profile.json', 'step7-next/life-profile.json')
source = source.replace('life-30-final', 'life-30-final-v2')
source = source.replace("'scope': 'Retained world/player; no initial task; isolated config; natural mobs enabled.'",
    "'scope': 'Thirty-minute life-profile test in retained artificial platform world; real SDK Agent and Decision; natural mobs enabled; controlled Visitor, time and a mid-run user goal. Retained inventory.'")
exec(compile(source, __file__, 'exec'))
