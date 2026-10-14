import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import pytest

root = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(root))
directory = Path(tempfile.mkdtemp(prefix='minebot-connection-before-'))
source = subprocess.check_output(['git', 'show', '353cb9d:minebot/app/body_loop.py'], cwd=root, text=True)
path = directory / 'body_loop.py'
path.write_text(source)
spec = importlib.util.spec_from_file_location('old_connection_loop', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.FACT_FRAME_INTERVAL_S = .001
import minebot.app.body_loop as current
current.BodyLoop = module.BodyLoop
raise SystemExit(pytest.main([
    str(root / 'tests/unit/test_body_loop_connection.py'), '-q',
    '-k', 'disconnect_finishes or immediate_reconnect',
]))
