"""Restart only the owned session/client; keep test server and world."""
import json
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/start-life-fixed-g.py').read_text()
source = source.replace('step7-fixed-g', 'step7-fixed-h')
source = source.replace('for port in (25569, 25579, 8771):', 'for port in (8771,):')
begin = source.index("with (RUN / 'server-launch.log').open('wb') as log:")
end = source.index('profile = json.loads(', begin)
owned = json.loads((root / 'logs/reflex-always-on-20261014-step7-fixed-g/acceptance-run.json').read_text())
source = source[:begin] + "from types import SimpleNamespace\nserver = SimpleNamespace(pid=" + str(owned['server_pid']) + ")\n" + source[end:]
exec(compile(source, __file__, 'exec'))
