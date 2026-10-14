"""Future full suites: record the child exit directly, without shell quoting."""
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path('/home/qwait/MineBot-always-on')
output = Path(sys.argv[1]).resolve()
output.mkdir(exist_ok=False)
started = time.monotonic()
with (output / 'pytest.log').open('wb') as log:
    child = subprocess.run([sys.executable, '-m', 'pytest', 'tests/unit', '-q'],
        cwd=root, stdout=log, stderr=subprocess.STDOUT)
record = {'pytest_exit_code': child.returncode, 'elapsed_s': time.monotonic()-started,
    'commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()}
(output / 'exit.json').write_text(json.dumps(record, indent=2))
print(json.dumps(record))
raise SystemExit(child.returncode)
