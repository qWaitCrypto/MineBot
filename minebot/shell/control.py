"""The launcher's control channel: a Unix socket that any number of front ends can drive.

The supervisor process owns one :class:`ControlServer` around its :class:`Launcher`; the
panel that ``minebot`` opens and every ``minebot attach`` talk to it through
:class:`RemoteLauncher`. Requests and replies are JSON lines:

    {"id": "1", "op": "send", "text": "come here"}
    {"id": "1", "ok": true, "error": null}

Operations: ``snapshot`` (launch steps, phase, process state), ``send`` (one operator line,
exactly what the session would read from its terminal), ``retry`` (rerun the failed launch
step) and ``stop`` (``text`` = ``keep_client`` keeps the game client running). Every ``send``
and ``stop`` is appended to ``control.log`` in the run directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

SOCKET_NAME = "control.sock"
OPS = ("snapshot", "send", "retry", "restart_client", "stop")
_READ_LIMIT = 1 << 16
_SUN_PATH_MAX = 100  # sockaddr_un.sun_path is 104 bytes on macOS, 108 on Linux


def socket_path(run_dir: Path) -> Path:
    return Path(run_dir) / SOCKET_NAME


class ControlError(Exception):
    pass


class ControlServer:
    """Serves one launcher over ``<run_dir>/control.sock``."""

    def __init__(self, launcher: Any, *, on_stop: Callable[[bool], None] | None = None,
                 on_retry: Callable[[], None] | None = None,
                 on_restart_client: Callable[[], None] | None = None) -> None:
        self.launcher = launcher
        self.path = socket_path(launcher.run_dir)
        self.log_path = Path(launcher.run_dir) / "control.log"
        self.on_stop = on_stop
        self.on_retry = on_retry
        self.on_restart_client = on_restart_client
        self.listening = False
        self._bound: Path | None = None
        self._server: socket.socket | None = None
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._peers = 0

    @property
    def peers(self) -> int:
        """Front ends connected right now."""
        return self._peers

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bound = self.path
        if len(str(self.path).encode()) > _SUN_PATH_MAX:
            # Too long for a socket address: bind in the temp dir and leave a symlink in the run dir.
            digest = hashlib.sha1(str(self.path).encode()).hexdigest()[:12]
            bound = Path(tempfile.gettempdir()) / f"minebot-{digest}.sock"
        for stale in {bound, self.path}:
            if stale.is_symlink() or stale.exists():
                stale.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(bound))
        os.chmod(bound, 0o600)
        if bound != self.path:
            self.path.symlink_to(bound)
        server.listen(8)
        server.settimeout(0.5)
        self._server, self._bound, self.listening = server, bound, True
        threading.Thread(target=self._serve, name="minebot-control", daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        for path in {self.path, self._bound}:
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
        self.listening = False

    # ------------------------------------------------------------ serving
    def _serve(self) -> None:
        while not self._closed.is_set():
            server = self._server
            if server is None:
                return
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), name="minebot-control-peer", daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        with self._lock:
            self._peers += 1
        try:
            with connection:
                connection.settimeout(1.0)
                buffer = b""
                while not self._closed.is_set():
                    try:
                        chunk = connection.recv(4096)
                    except TimeoutError:
                        continue
                    if not chunk:
                        return
                    buffer += chunk
                    if len(buffer) > _READ_LIMIT:
                        return
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        reply = self.reply(line.decode("utf-8", "replace"))
                        connection.sendall(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        except OSError:
            return
        finally:
            with self._lock:
                self._peers -= 1

    def reply(self, line: str) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"id": None, "ok": False, "error": f"请求不是 JSON：{exc}"}
        if not isinstance(request, dict):
            return {"id": None, "ok": False, "error": "请求必须是 JSON 对象"}
        request_id = request.get("id")
        op, text = str(request.get("op") or ""), str(request.get("text") or "")
        try:
            result = self.apply(op, text)
        except ControlError as exc:
            if op != "snapshot":
                self._log(op, text, str(exc))
            return {"id": request_id, "ok": False, "error": str(exc)}
        if op != "snapshot":
            self._log(op, text, None)
        return {"id": request_id, "ok": True, "error": None, **result}

    def apply(self, op: str, text: str = "") -> dict[str, Any]:
        launcher = self.launcher
        if op == "snapshot":
            return {"snapshot": launcher.snapshot()}
        if op == "send":
            if not text.strip():
                raise ControlError("内容为空")
            if not launcher.send(text):
                raise ControlError("Agent 会话不在运行")
            return {}
        if op == "retry":
            if launcher.phase != "failed":
                raise ControlError("启动没有失败，不需要重试")
            if self.on_retry is None:
                raise ControlError("这个启动器不支持远程重试")
            self.on_retry()
            return {}
        if op == "restart_client":
            if self.on_restart_client is None:
                raise ControlError("这个启动器不支持远程重启客户端")
            if launcher.client_alive:
                raise ControlError("客户端还在运行")
            if launcher.client_reused:
                raise ControlError("这个客户端不是我启动的，不自动重启它")
            self.on_restart_client()
            return {}
        if op == "stop":
            if self.on_stop is None:
                raise ControlError("这个启动器不支持远程停止")
            self.on_stop(text == "keep_client")
            return {}
        raise ControlError(f"未知操作 {op!r}")

    def _log(self, op: str, text: str, error: str | None) -> None:
        record = {"ts": round(time.time(), 3), "op": op, "text": text, "ok": error is None, "error": error}
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


class ControlClient:
    """One connection to a control socket; requests are serialized, so threads may share it."""

    def __init__(self, run_dir: Path, *, timeout_s: float = 3.0) -> None:
        self.path = socket_path(run_dir)
        self.timeout_s = timeout_s
        self._socket: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.Lock()
        self._next_id = 0

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout_s)
        target = os.path.realpath(self.path)  # long run dirs hold a symlink to a short temp-dir socket
        try:
            connection.connect(target)
        except OSError as exc:
            connection.close()
            raise ControlError(f"连不上 {self.path}：{exc}") from exc
        self._socket, self._buffer = connection, b""

    def close(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def request(self, op: str, text: str = "") -> dict[str, Any]:
        with self._lock:
            if self._socket is None:
                raise ControlError("控制通道未连接")
            self._next_id += 1
            body: dict[str, Any] = {"id": str(self._next_id), "op": op}
            if text:
                body["text"] = text
            try:
                self._socket.sendall(json.dumps(body, ensure_ascii=False).encode("utf-8") + b"\n")
                while b"\n" not in self._buffer:
                    chunk = self._socket.recv(65536)
                    if not chunk:
                        raise ControlError("启动器已断开")
                    self._buffer += chunk
            except OSError as exc:
                self.close()
                raise ControlError(f"启动器已断开：{exc}") from exc
            line, self._buffer = self._buffer.split(b"\n", 1)
        reply = json.loads(line.decode("utf-8", "replace"))
        if not isinstance(reply, dict):
            raise ControlError("启动器返回了意外内容")
        return reply


class RemoteLauncher:
    """The :class:`~minebot.shell.launch.Launcher` surface the shell app needs, over the socket.

    A background thread mirrors the supervisor's snapshot every ``poll_s``; control calls go
    straight to the socket. When the supervisor goes away after stopping, ``phase`` becomes
    ``stopped``; when it vanishes otherwise, ``lost``.
    """

    detachable = True

    def __init__(self, run_dir: Path, *, poll_s: float = 0.2) -> None:
        from minebot.shell.launch import STEPS, Step

        self.run_dir = Path(run_dir).resolve()
        self.trace_path = self.run_dir / "trace.jsonl"
        self.agent_log = self.run_dir / "agent.log"
        self.client_log = self.run_dir / "client.log"
        self.steps = [Step(key, label) for key, label in STEPS]
        self.phase = "launching"
        self.output: deque[str] = deque(maxlen=6)
        self.messages: deque[tuple[float, str, str]] = deque(maxlen=12)
        self.bot = ""
        self.server = ""
        self.started_at = time.time()
        self.agent_paused = False
        self.session_returncode: int | None = None
        self.client_started = False
        self.client_alive = True
        self._alive = False
        self._poll_s = poll_s
        self._client = ControlClient(self.run_dir)
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        self._client.connect()
        self.refresh()
        self._thread = threading.Thread(target=self._poll, name="minebot-attach", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stopped.set()
        self._client.close()

    def _poll(self) -> None:
        while not self._stopped.wait(self._poll_s):
            try:
                self.refresh()
            except (ControlError, ValueError):
                self.phase = "stopped" if self.phase in ("stopping", "stopped") else "lost"
                self._alive = False
                return

    def refresh(self) -> None:
        reply = self._client.request("snapshot")
        self._apply(reply.get("snapshot") or {})

    def _apply(self, snapshot: dict[str, Any]) -> None:
        now = time.monotonic()
        for step, data in zip(self.steps, snapshot.get("steps") or []):
            step.status, step.summary = data.get("status", "pending"), data.get("summary", "")
            step.lines = [tuple(line) for line in data.get("lines") or []]
            step.error, step.hint = data.get("error", ""), data.get("hint", "")
            elapsed = data.get("elapsed_s")
            if elapsed is None:
                step.started = step.ended = None
            else:
                step.started = now - float(elapsed)
                step.ended = None if step.status == "running" else now
        self.output = deque(snapshot.get("output") or [], maxlen=6)
        self.messages = deque((tuple(message) for message in snapshot.get("messages") or []), maxlen=12)
        self.bot, self.server = snapshot.get("bot", ""), snapshot.get("server", "")
        self.started_at = float(snapshot.get("started_at") or self.started_at)
        self.agent_paused = bool(snapshot.get("agent_paused"))
        self.session_returncode = snapshot.get("session_returncode")
        self.client_started = bool(snapshot.get("client_started"))
        self.client_alive = bool(snapshot.get("client_alive", True))
        self._alive = bool(snapshot.get("session_alive"))
        self.phase = snapshot.get("phase", self.phase)

    # ------------------------------------------------------------ launcher surface
    @property
    def current(self) -> Any:
        return next((step for step in self.steps if step.status in ("running", "fail")), None)

    def session_alive(self) -> bool:
        return self._alive

    def restart_client(self) -> bool:
        """Ask the supervisor to start the game client again after a crash."""
        try:
            return bool(self._client.request("restart_client").get("ok"))
        except ControlError:
            return False

    def send(self, line: str) -> bool:
        text = line.replace("\n", " ").strip()
        if not text:
            return False
        try:
            reply = self._client.request("send", text)
        except ControlError:
            return False
        if reply.get("ok"):
            lowered = text.lower()
            if lowered.startswith("/pause"):
                self.agent_paused = True
            elif lowered.startswith(("/continue", "/cancel", "/stop")):
                self.agent_paused = False
        return bool(reply.get("ok"))

    def run(self, start: int | None = None, stop: int | None = None) -> bool:
        """Ask the supervisor to retry the failed step; progress arrives through snapshots."""
        del start, stop
        try:
            reply = self._client.request("retry")
        except ControlError:
            return False
        if reply.get("ok"):
            self.phase = "launching"
        return bool(reply.get("ok"))

    def shutdown(self, *, stop_client: bool = True, timeout_s: float = 90.0) -> None:
        """Stop the supervisor and wait until it has finished shutting everything down."""
        if self.phase in ("stopped", "lost"):
            return
        try:
            self._client.request("stop", "" if stop_client else "keep_client")
        except ControlError:
            return
        self.phase = "stopping"
        deadline = time.monotonic() + timeout_s
        while self.phase not in ("stopped", "lost") and time.monotonic() < deadline:
            time.sleep(0.1)


def live_runs(root: Path) -> list[Path]:
    """Session directories whose supervisor answers on its control socket, newest first."""
    sessions = Path(root) / "logs" / "sessions"
    if not sessions.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(sessions.iterdir(), reverse=True):
        if not socket_path(path).exists():
            continue
        client = ControlClient(path, timeout_s=0.5)
        try:
            client.connect()
            client.request("snapshot")
        except (ControlError, ValueError):
            continue
        finally:
            client.close()
        found.append(path.resolve())
    return found


def resolve_run(root: Path, name: str | None) -> Path:
    """``minebot attach [run]``: a run directory, a session name, or the newest live session."""
    sessions = Path(root) / "logs" / "sessions"
    if not name:
        live = live_runs(root)
        if not live:
            raise ControlError("没有正在运行的 MineBot 会话")
        return live[0]
    for candidate in (Path(name).expanduser(), sessions / name):
        if candidate.is_dir():
            return candidate.resolve()
    matches = sorted(sessions.glob(f"*{name}*")) if sessions.is_dir() else []
    if len(matches) == 1:
        return matches[0].resolve()
    if matches:
        raise ControlError(f"{name!r} 匹配到多个会话：{', '.join(path.name for path in matches)}")
    raise ControlError(f"找不到会话 {name!r}")


__all__ = ["OPS", "SOCKET_NAME", "ControlClient", "ControlError", "ControlServer", "RemoteLauncher",
           "live_runs", "resolve_run", "socket_path"]
