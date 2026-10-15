"""Run the new end-to-end regression against the pre-fix implementation."""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(root))
with tempfile.TemporaryDirectory(prefix='minebot-running-perception-old-') as temporary:
    source = Path(temporary) / 'body_loop.py'
    source.write_bytes(subprocess.check_output(['git', 'show', 'edc89ff:minebot/app/body_loop.py'], cwd=root))
    spec = importlib.util.spec_from_file_location('minebot.app.body_loop_old', source)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    spec = importlib.util.spec_from_file_location('running_perception_regression',
        root / 'tests/unit/test_body_loop_running_perception.py')
    tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tests)
    tests.BodyLoop = old.BodyLoop
    tests.test_running_question_reads_new_attacker_instead_of_carrying_empty_entities()
