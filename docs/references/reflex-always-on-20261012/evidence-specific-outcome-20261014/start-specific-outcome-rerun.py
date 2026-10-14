"""Same owned server, new SDK state; allow cached offline client startup."""
from pathlib import Path
root=Path('/home/qwait/MineBot-always-on')
source=(root/'logs/start-life-fixed-g.py').read_text()
source=source.replace('step7-fixed-g','specific-outcome-rerun').replace('Step3CurrentBot','Step5NextBot')
source=source.replace('step7-current-f/life-profile.json','step7-next/life-profile.json')
source=source.replace('for port in (25569, 25579, 8771):','for port in (8771,):')
start=source.index("with (RUN / 'server-launch.log').open('wb') as log:")
end=source.index('profile = json.loads(',start)
source=source[:start]+"from types import SimpleNamespace\nserver=SimpleNamespace(pid=2290854)\nassert Path('/proc/2290854').exists()\n"+source[end:]
source=source.replace('logs/step2-live-client.sh','logs/specific-outcome-offline-client.sh')
source=source.replace("'scope': 'Retained world/player; no initial task; isolated config; natural mobs enabled.'",
    "'scope': 'Corrected harness after 240s startup window expired; reuse owned server 2290854 from specific-outcome; retained artificial platform; fresh SDK state; no acceptance evidence from aborted startup.'")
exec(compile(source,__file__,'exec'))
