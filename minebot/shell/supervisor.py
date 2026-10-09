"""The background half of ``minebot``: one launcher, supervised outside the terminal.

``minebot`` starts this module as its own process session, then opens the panel as a
front end over ``<run_dir>/control.sock``. Closing or crashing the panel leaves the bot
running; ``minebot attach`` opens a new front end. The supervisor exits after an orderly
stop, on SIGTERM/SIGHUP, or when no front end has been attached for :data:`ORPHAN_S`
while the launch has failed or the Agent session has ended.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from minebot.shell.control import ControlError, ControlServer, socket_path
from minebot.shell.launch import LaunchOptions, Launcher

ORPHAN_S = 60.0
LINGER_S = 3.0  # keep answering after stopping so front ends see the final messages
OPTIONS_NAME = "launcher-options.json"


def write_options(path: Path, options: LaunchOptions, *, client_command: list[str] | None = None,
                  session_command: list[str] | None = None) -> None:
    data = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(options).items()}
    data["client_command"], data["session_command"] = client_command, session_command
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_options(path: Path) -> tuple[LaunchOptions, list[str] | None, list[str] | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    paths = {"env_file", "run_dir", "servers_file", "accounts_dir"}
    kwargs = {item.name: (Path(data[item.name]) if item.name in paths and data.get(item.name) else data.get(item.name))
              for item in fields(LaunchOptions) if item.name in data}
    return LaunchOptions(**kwargs), data.get("client_command"), data.get("session_command")


class Supervisor:
    def __init__(self, launcher: Launcher, *, orphan_s: float = ORPHAN_S) -> None:
        self.launcher = launcher
        self.orphan_s = orphan_s
        self.server = ControlServer(launcher, on_stop=self.request_stop, on_retry=self.retry)
        self._stop = threading.Event()
        self._keep_client = False
        self._run_thread: threading.Thread | None = None

    def request_stop(self, keep_client: bool = False) -> None:
        self._keep_client = keep_client
        self._stop.set()

    def retry(self) -> None:
        if self._run_thread is not None and self._run_thread.is_alive():
            raise ControlError("正在启动中")
        self._start_run()

    def _start_run(self) -> None:
        self._run_thread = threading.Thread(target=self.launcher.run, name="minebot-launch", daemon=True)
        self._run_thread.start()

    def serve(self) -> int:
        self.server.start()
        self._start_run()
        alone_since: float | None = None
        while not self._stop.wait(0.25):
            launcher = self.launcher
            finished = launcher.phase == "failed" or (launcher.session is not None and not launcher.session_alive())
            if self.server.peers or not finished:
                alone_since = None
            elif alone_since is None:
                alone_since = time.monotonic()
            elif time.monotonic() - alone_since > self.orphan_s:
                launcher.say("muted", f"{self.orphan_s:.0f} 秒没有面板连着，自动收尾")
                break
        self.launcher.shutdown(stop_client=not self._keep_client)
        deadline = time.monotonic() + LINGER_S
        while self.server.peers and time.monotonic() < deadline:
            time.sleep(0.1)
        self.server.close()
        return 0


def spawn(options: LaunchOptions, *, client_command: list[str] | None = None,
          session_command: list[str] | None = None, timeout_s: float = 15.0) -> subprocess.Popen[bytes]:
    """Start a supervisor for ``options.run_dir`` and wait until its control socket answers."""
    assert options.run_dir is not None
    # The supervisor runs from the repository root; paths typed on the command line are relative to here.
    for name in ("env_file", "servers_file", "accounts_dir"):
        value = getattr(options, name)
        if value is not None:
            setattr(options, name, Path(value).expanduser().resolve())
    run_dir = options.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    write_options(run_dir / OPTIONS_NAME, options, client_command=client_command, session_command=session_command)
    with open(run_dir / "launcher.log", "ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "minebot.shell.supervisor", str(run_dir)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, cwd=Path(__file__).resolve().parents[2])
    deadline = time.monotonic() + timeout_s
    while not socket_path(run_dir).exists():
        if process.poll() is not None:
            raise ControlError(f"启动器进程退出了（代码 {process.returncode}），看 {run_dir / 'launcher.log'}")
        if time.monotonic() > deadline:
            process.kill()
            raise ControlError(f"启动器 {timeout_s:.0f} 秒没有就绪，看 {run_dir / 'launcher.log'}")
        time.sleep(0.05)
    return process


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m minebot.shell.supervisor RUN_DIR", file=sys.stderr)
        return 2
    run_dir = Path(argv[0]).resolve()
    options, client_command, session_command = read_options(run_dir / OPTIONS_NAME)
    options.run_dir = run_dir
    launcher = Launcher(options, launch_client_command=client_command, session_command=session_command)
    supervisor = Supervisor(launcher)

    def terminate(*_: Any) -> None:
        supervisor.request_stop(False)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, terminate)
    print(f"supervisor pid={os.getpid()} run_dir={run_dir}", flush=True)
    code = supervisor.serve()
    print(f"supervisor stopped phase={launcher.phase}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ORPHAN_S", "Supervisor", "main", "read_options", "spawn", "write_options"]
