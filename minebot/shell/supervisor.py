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
from minebot.shell.launch import HOME_VAR, ROOT, LaunchOptions, Launcher

ORPHAN_S = 60.0
# An Agent process that dies is started again after a short, growing delay,
# at most this many times within the window.
AGENT_RESTART_DELAY_S = 2.0
AGENT_RESTART_LIMIT = 4
AGENT_RESTART_WINDOW_S = 1800.0
# The game client is the Body. One that exits while the bot runs is started
# again the same way.
CLIENT_RESTART_DELAY_S = 5.0
CLIENT_RESTART_LIMIT = 4
CLIENT_RESTART_WINDOW_S = 1800.0
# A client of ours still running but off the server this long is started
# again, which joins the server the way it does at launch.
CLIENT_REJOIN_AFTER_S = 30.0
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
        self.server = ControlServer(launcher, on_stop=self.request_stop, on_retry=self.retry,
                                    on_restart_client=self.restart_client)
        self._stop = threading.Event()
        self._keep_client = False
        self._run_thread: threading.Thread | None = None
        self._client_thread: threading.Thread | None = None
        self._agent_thread: threading.Thread | None = None
        self._agent_restarts: list[float] = []
        self._agent_next_restart = 0.0
        self._agent_gave_up = False
        self._client_restarts: list[float] = []
        self._client_next_restart = 0.0
        self._client_gave_up = False

    def request_stop(self, keep_client: bool = False) -> None:
        self._keep_client = keep_client
        self._stop.set()

    def restart_client(self) -> None:
        if self._client_thread is not None and self._client_thread.is_alive():
            raise ControlError("客户端正在启动中")
        self._client_thread = threading.Thread(target=self.launcher.restart_client, name="minebot-client-restart",
                                               daemon=True)
        self._client_thread.start()

    def _keep_agent_alive(self) -> None:
        """An Agent that ended without being asked to is started again.

        Exit code 0 is the Agent quitting on request. Anything else is a
        crash: the Body would stand in the world with nobody behind it. A
        process that keeps dying is left down after a few tries, and says so.
        """
        launcher = self.launcher
        session = launcher.session
        if (launcher.phase != "ready" or session is None or launcher.session_alive()
                or session.returncode == 0 or self._stop.is_set()):
            return
        if self._agent_thread is not None and self._agent_thread.is_alive():
            return
        now = time.monotonic()
        self._agent_restarts = [at for at in self._agent_restarts if now - at < AGENT_RESTART_WINDOW_S]
        if len(self._agent_restarts) >= AGENT_RESTART_LIMIT:
            if not self._agent_gave_up:
                self._agent_gave_up = True
                launcher.say("fail", f"Agent 在 {AGENT_RESTART_WINDOW_S / 60:.0f} 分钟内退出了 "
                                     f"{AGENT_RESTART_LIMIT} 次，不再自动拉起，看 agent.log")
            return
        if now < self._agent_next_restart:
            return
        self._agent_restarts.append(now)
        self._agent_next_restart = now + AGENT_RESTART_DELAY_S * (2 ** len(self._agent_restarts))
        self._agent_thread = threading.Thread(target=launcher.restart_session, name="minebot-agent-restart",
                                              daemon=True)
        self._agent_thread.start()

    def _keep_client_alive(self) -> None:
        """A game client that exited while the bot runs is started again.

        The launcher stops the client only on an orderly stop, so any other
        exit leaves the Body out of the world: in the house run (2026-10-17)
        the window closed at 00:31 and the Agent waited 13 hours for a Body
        nobody brought back. A reused client is not ours to start.
        """
        launcher = self.launcher
        if (launcher.phase != "ready" or launcher.client_alive or self._stop.is_set()
                or (launcher.client is None and not self._client_restarts)):
            return
        code = launcher.client.returncode if launcher.client is not None else None
        self._restart_client_counted(lambda count: f"游戏客户端意外退出（代码 {code}），正在第 {count} 次重新拉起")

    def _rejoin_lost_client(self) -> None:
        """A client of ours that lost the server and stays off it is started again.

        Nothing else brings it back: in H5 (2026-10-29) a server stall
        dropped the client at 10:39; it stayed at the menu, alive, so
        ``_keep_client_alive`` had nothing to do, and the Body never came
        back (s7-later-20261029.md §8.15). A new client joins the server the
        way it does at launch. It counts with the restarts above.
        """
        launcher = self.launcher
        lost = launcher.connection
        if (launcher.phase != "ready" or self._stop.is_set() or launcher.client is None
                or not launcher.client_alive or lost.get("state") != "lost"
                or time.time() - float(lost.get("since") or 0.0) < CLIENT_REJOIN_AFTER_S):
            return
        reason = lost.get("reason") or "?"

        def rejoin() -> None:
            launcher.stop_client()
            launcher.restart_client()

        if self._restart_client_counted(
                lambda count: f"游戏客户端掉线 {CLIENT_REJOIN_AFTER_S:.0f} 秒还没回到服务器（{reason}），"
                              f"正在第 {count} 次重启客户端重新进服", target=rejoin):
            launcher.connection = {**lost, "state": "rejoining"}

    def _restart_client_counted(self, message, *, target=None) -> bool:
        """Start the client again in the background, within the restart limits."""
        launcher = self.launcher
        if self._client_thread is not None and self._client_thread.is_alive():
            return False
        now = time.monotonic()
        self._client_restarts = [at for at in self._client_restarts if now - at < CLIENT_RESTART_WINDOW_S]
        if len(self._client_restarts) >= CLIENT_RESTART_LIMIT:
            if not self._client_gave_up:
                self._client_gave_up = True
                launcher.say("fail", f"游戏客户端在 {CLIENT_RESTART_WINDOW_S / 60:.0f} 分钟内退出了 "
                                     f"{CLIENT_RESTART_LIMIT} 次，不再自动拉起，看 client.log")
            return False
        if now < self._client_next_restart:
            return False
        self._client_restarts.append(now)
        self._client_next_restart = now + CLIENT_RESTART_DELAY_S * (2 ** len(self._client_restarts))
        launcher.say("fail", message(len(self._client_restarts)))
        self._client_thread = threading.Thread(target=target or launcher.restart_client,
                                               name="minebot-client-restart", daemon=True)
        self._client_thread.start()
        return True

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
            launcher.watch_client_log()
            self._keep_agent_alive()
            self._keep_client_alive()
            self._rejoin_lost_client()
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
                                   start_new_session=True, cwd=ROOT, env={**os.environ, HOME_VAR: str(ROOT)})
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
