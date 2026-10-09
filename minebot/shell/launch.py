"""``minebot`` launch orchestration: profile → server → client → world → Agent, then supervise and stop.

Blocking code; the shell app runs :meth:`Launcher.run` in a worker thread and renders
:attr:`Launcher.steps` every frame. The Agent session is the unchanged
``minebot.app.real_server_session --interactive`` process; operator input reaches it
through its stdin, the same channel as typing into that process directly.

:mod:`minebot.shell.supervisor` runs one launcher in a background process and serves it
over :mod:`minebot.shell.control`, so the panel can detach and ``minebot attach`` back.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from minebot.app.config import AppConfigError, provider_registry_from_env
from minebot.app.local_launcher import LocalLauncherError, discover_runtime_env_path, load_runtime_environment
from minebot.app.model_provider import ProviderConfigError
from minebot.app.real_server_session import RealServerConfigError, real_server_config_from_env
from minebot.game.real_client_ipc import RealClientIpc, RealClientIpcError
from minebot.panel.source import TraceFollower
from minebot.shell.accounts import AccountError, AccountStore
from minebot.shell.profiles import ProfileError, ServerBook
from minebot.shell.slp import PingError, ping, split_address

HOME_VAR = "MINEBOT_HOME"


def find_home(environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> Path:
    """The MineBot checkout that holds tools/, minecraft/ and logs/.

    ``MINEBOT_HOME`` wins; then the current directory or a parent that looks like a checkout;
    then the source tree this module came from (an editable install or running from source).
    An installed package's own directory is never used: it has no client and no logs/.
    """
    environment = os.environ if environ is None else environ
    if environment.get(HOME_VAR):
        return Path(environment[HOME_VAR]).expanduser().resolve()
    here = (cwd or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if _is_checkout(candidate):
            return candidate
    return Path(__file__).resolve().parents[2]


def _is_checkout(path: Path) -> bool:
    return (path / "tools" / "open-client.sh").is_file() and (path / "minecraft").is_dir()


ROOT = find_home()
STEPS = (
    ("env", "环境"),
    ("account", "账号"),
    ("server", "服务器"),
    ("client", "客户端"),
    ("world", "进服"),
    ("agent", "Agent"),
)
_NAME = re.compile(r"[A-Za-z0-9_]{1,16}")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SHELL_DEFAULTS = {
    "MINEBOT_BODY_PROVIDER": "real_client",
    "MINEBOT_REAL_CLIENT_HOST": "127.0.0.1",
    "MINEBOT_AGENT_LANGUAGE": "Chinese",
    "MINEBOT_CLIENT_SERVER": "127.0.0.1:25566",
}
_CLIENT_MILESTONES = (
    ("client_body_loaded", "mod 已加载"),
    ("ipc_listening", "IPC 开始监听"),
    ("Connecting to", "正在连接服务器"),
    ("MINEBOT_PHASE_A ready", "角色已进入世界"),
)
_DISCONNECT = re.compile(r"Client disconnected with reason: (.*)$")
_CLIENT_SECRETS = ("MINEBOT_CLIENT_ACCESS_TOKEN", "MINEBOT_CLIENT_UUID")
_ERROR_MARKERS = ("ERROR", "Exception", "FAILURE", "Error:", "Caused by")


@dataclass
class Step:
    key: str
    label: str
    status: str = "pending"  # pending | running | ok | warn | fail
    summary: str = ""
    lines: list[tuple[str, str]] = field(default_factory=list)  # (tone, text)
    started: float | None = None
    ended: float | None = None
    error: str = ""
    hint: str = ""

    def elapsed(self, now: float | None = None) -> float | None:
        if self.started is None:
            return None
        return (self.ended or now or time.monotonic()) - self.started

    def note(self, tone: str, text: str) -> None:
        self.lines.append((tone, text))


@dataclass
class LaunchOptions:
    goal: str | None = None
    env_file: Path | None = None
    run_dir: Path | None = None
    server: str | None = None
    bot: str | None = None
    port: int | None = None
    disposable: bool | None = None
    launch_client: bool = True
    max_steps: int | None = None
    client_timeout_s: float = 300.0
    world_timeout_s: float = 180.0
    agent_timeout_s: float = 120.0
    create_run_dir: bool = True
    profile: str | None = None  # servers.toml entry; None = the default one, if any
    servers_file: Path | None = None
    accounts_dir: Path | None = None


class StepFailed(Exception):
    def __init__(self, error: str, hint: str = "") -> None:
        super().__init__(error)
        self.error = error
        self.hint = hint


def default_run_dir(root: Path = ROOT) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / "logs" / "sessions" / stamp


def port_open(host: str, port: int, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def tail_lines(path: Path, count: int, *, max_bytes: int = 64 << 10) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line for line in data.splitlines() if line.strip()][-count:]


def error_lines(path: Path, count: int = 4) -> list[str]:
    return [line for line in tail_lines(path, 400) if any(marker in line for marker in _ERROR_MARKERS)][-count:]


def shell_environment(environment: Mapping[str, str], options: LaunchOptions) -> dict[str, str]:
    """The runtime profile plus launcher overrides, with the client and session agreeing on bot and port."""
    result = dict(environment)
    for key, value in _SHELL_DEFAULTS.items():
        result.setdefault(key, value)
    if options.bot:
        result["MINEBOT_REAL_BOT"] = options.bot
    if options.server:
        result["MINEBOT_CLIENT_SERVER"] = options.server
    if options.port:
        result["MINEBOT_REAL_CLIENT_PORT"] = str(options.port)
    if options.disposable is not None:
        result["MINEBOT_CLIENT_DISPOSABLE_WORLD"] = "true" if options.disposable else "false"
    bot = result.get("MINEBOT_REAL_BOT") or result.get("MINEBOT_CLIENT_USERNAME") or ""
    if bot:
        result["MINEBOT_REAL_BOT"] = result["MINEBOT_CLIENT_USERNAME"] = bot
    port = result.get("MINEBOT_REAL_CLIENT_PORT") or result.get("MINEBOT_CLIENT_IPC_PORT") or "8768"
    result["MINEBOT_REAL_CLIENT_PORT"] = result["MINEBOT_CLIENT_IPC_PORT"] = port
    # The production session never constructs RCON; keep test-server credentials out of it.
    # Account tokens are added for the game client alone, never from a profile file.
    for key in [key for key in result if "RCON" in key.upper() or key in _CLIENT_SECRETS]:
        result.pop(key)
    return result


def _shell_overrides(path: Path | None) -> list[str]:
    """Keys set in both the profile file and this process's environment with different values."""
    if path is None:
        return []
    try:
        from_file = load_runtime_environment(path, environ={})
    except LocalLauncherError:
        return []
    return sorted(key for key, value in from_file.items() if key in os.environ and os.environ[key] != value)


class Launcher:
    def __init__(self, options: LaunchOptions, *, root: Path = ROOT, launch_client_command: list[str] | None = None,
                 session_command: list[str] | None = None) -> None:
        self.options = options
        self.root = root
        self.steps = [Step(key, label) for key, label in STEPS]
        self.phase = "launching"  # launching | ready | failed | stopping | stopped
        self.output: deque[str] = deque(maxlen=6)
        self.messages: deque[tuple[float, str, str]] = deque(maxlen=12)  # shutdown progress
        self.environment: dict[str, str] = {}
        self.env_path: Path | None = None
        self.run_dir = (options.run_dir or default_run_dir(root)).expanduser().resolve()
        self.trace_path = self.run_dir / "trace.jsonl"
        self.client_log = self.run_dir / "client.log"
        self.agent_log = self.run_dir / "agent.log"
        self.client: subprocess.Popen[bytes] | None = None
        self.client_reused = False
        self.profile_name = ""
        self.connection = {"state": "unknown", "reason": "", "since": 0.0}  # from client.log; see watch_client_log
        self._log_offset = 0
        self.account_kind = "offline"
        self._client_secrets: dict[str, str] = {}  # only the game client gets these
        self.session: subprocess.Popen[bytes] | None = None
        self.server_info: dict[str, Any] = {}
        self.body: dict[str, Any] = {}
        self.model = ""
        self.started_at = time.time()
        self.agent_paused = False
        self._launch_client_command = launch_client_command
        self._session_command = session_command
        self._stdin_lock = threading.Lock()
        self._cancel = threading.Event()
        self._failed_index = 0

    # ------------------------------------------------------------ properties
    @property
    def current(self) -> Step | None:
        for step in self.steps:
            if step.status in ("running", "fail"):
                return step
        return None

    @property
    def bot(self) -> str:
        return self.environment.get("MINEBOT_REAL_BOT", "")

    @property
    def server(self) -> str:
        return self.environment.get("MINEBOT_CLIENT_SERVER", "")

    @property
    def ipc(self) -> tuple[str, int]:
        return (self.environment.get("MINEBOT_REAL_CLIENT_HOST", "127.0.0.1"),
                int(self.environment.get("MINEBOT_REAL_CLIENT_PORT", "8768")))

    @property
    def disposable(self) -> bool:
        return self.environment.get("MINEBOT_CLIENT_DISPOSABLE_WORLD", "false") == "true"

    def session_alive(self) -> bool:
        return self.session is not None and self.session.poll() is None

    @property
    def session_returncode(self) -> int | None:
        return self.session.poll() if self.session is not None else None

    @property
    def client_started(self) -> bool:
        return self.client is not None

    @property
    def client_alive(self) -> bool:
        """Our client process is running. A reused client is assumed alive: probing its IPC port
        could take the single connection slot the Agent session needs."""
        if self.client is not None:
            return self.client.poll() is None
        return self.client_reused

    def snapshot(self) -> dict[str, Any]:
        """Everything a remote front end needs to draw the launch screen and drive the panel."""
        now = time.monotonic()
        return {
            "phase": self.phase,
            "bot": self.bot,
            "server": self.server,
            "run_dir": str(self.run_dir),
            "started_at": self.started_at,
            "steps": [{"key": step.key, "label": step.label, "status": step.status, "summary": step.summary,
                       "lines": [list(line) for line in step.lines], "error": step.error, "hint": step.hint,
                       "elapsed_s": step.elapsed(now)} for step in self.steps],
            "output": list(self.output),
            "messages": [list(message) for message in self.messages],
            "session_alive": self.session_alive(),
            "session_returncode": self.session_returncode,
            "client_started": self.client_started,
            "client_alive": self.client_alive,
            "connection": dict(self.connection),
            "disposable": self.disposable if self.environment else None,
            "server_version": self.server_info.get("version"),
            "client_reused": self.client_reused,
            "agent_paused": self.agent_paused,
        }

    # ------------------------------------------------------------ run
    def run(self, start: int | None = None, stop: int | None = None) -> bool:
        """Run steps ``start``..``stop`` (default: from the failed one to the end); True when all passed."""
        index = self._failed_index if start is None else start
        self.phase = "launching"
        runners: list[Callable[[Step], None]] = [
            self._step_env, self._step_account, self._step_server, self._step_client, self._step_world, self._step_agent,
        ]
        for position in range(index, len(self.steps) if stop is None else stop):
            if self._cancel.is_set():
                return False
            step = self.steps[position]
            step.status, step.lines, step.error, step.hint, step.summary = "running", [], "", "", ""
            step.started, step.ended = time.monotonic(), None
            self.output.clear()
            try:
                runners[position](step)
            except StepFailed as failure:
                step.status, step.error, step.hint = "fail", failure.error, failure.hint
                step.ended = time.monotonic()
                self._failed_index = position
                self.phase = "failed"
                self._write_record()
                return False
            except Exception as exc:  # an unexpected launcher bug must still surface as a failed step
                step.status, step.error = "fail", f"{type(exc).__name__}: {exc}"
                step.ended = time.monotonic()
                self._failed_index = position
                self.phase = "failed"
                self._write_record()
                return False
            if step.status == "running":
                step.status = "ok"
            step.ended = time.monotonic()
            if self._cancel.is_set():
                return False
        self.phase = "ready" if stop is None else "checked"
        self._write_record()
        return True

    def _step_env(self, step: Step) -> None:
        try:
            self.env_path = discover_runtime_env_path(self.options.env_file)
            loaded = load_runtime_environment(self.env_path)
        except LocalLauncherError as exc:
            raise StepFailed(str(exc), "用 --env-file 指定配置，或运行 minebot-local --init 生成模板") from exc
        options = self.options
        try:
            book = ServerBook(options.servers_file)
            if options.server and not options.profile and options.server in book.profiles:
                options = LaunchOptions(**{**vars(options), "profile": options.server, "server": None})  # --server home
            profile = book.get(options.profile)
        except ProfileError as exc:
            raise StepFailed(str(exc), "修好 servers.toml，或用 minebot servers 重新添加") from exc
        if options.profile and profile is None:
            raise StepFailed(f"没有服务器档案 {options.profile!r}",
                             f"已有：{', '.join(book.profiles) or '无'}；用 minebot servers add 添加")
        if profile is not None:
            # Command-line flags win over the profile, which wins over the runtime profile.
            options = LaunchOptions(**{**vars(options),
                                       "server": options.server or profile.address,
                                       "bot": options.bot or profile.account or None,
                                       "port": options.port or profile.port,
                                       "goal": options.goal or profile.goal or None,
                                       "disposable": options.disposable if options.disposable is not None
                                       else profile.disposable})
            self.options = options
            self.profile_name = profile.name
        self.environment = shell_environment(loaded, options)
        step.note("ok" if self.env_path else "warn", f"配置 {self.env_path or '未找到，使用当前环境变量'}")
        overridden = _shell_overrides(self.env_path)
        if overridden:
            step.note("warn", f"当前终端的环境变量覆盖了配置文件：{', '.join(overridden[:4])}"
                              + (f" 等 {len(overridden)} 项" if len(overridden) > 4 else ""))
        if profile is not None:
            step.note("ok", f"服务器档案 {profile.name}（{book.path}）")
        try:
            config = real_server_config_from_env(self.environment)
        except (RealServerConfigError, ValueError) as exc:
            raise StepFailed(f"会话配置无效：{exc}", "检查运行配置里的 MINEBOT_REAL_* 项") from exc
        try:
            registry = provider_registry_from_env(self.environment)
            try:
                registry.resolve("primary")
            finally:
                asyncio.run(registry.aclose())
        except (AppConfigError, ProviderConfigError, ValueError) as exc:
            raise StepFailed(f"模型配置无效：{exc}", "检查 MINEBOT_LLM_*；官方登录用 minebot-auth login") from exc
        self.model = self.environment.get("MINEBOT_LLM_MODEL") or self.environment.get("OPENAI_MODEL", "")
        auth = self.environment.get("MINEBOT_LLM_AUTH", "api_key")
        step.note("ok", f"模型 {self.model or '?'} · {'ChatGPT 登录' if auth == 'chatgpt' else 'API key'}"
                        f" · 推理 {self.environment.get('MINEBOT_LLM_REASONING_EFFORT', '默认')}")
        step.note("ok", f"身体 {config.body_provider} · IPC {config.real_client_host}:{config.real_client_port}")
        state_db = self.environment.get("MINEBOT_AGENT_STATE_DB") or "var/minebot/agent-state.sqlite3（跨会话保留）"
        step.note("muted", f"状态库 {state_db}")
        if self.options.launch_client:
            command = self.client_command()
            if command[:1] == ["bash"] and not Path(command[1]).is_file():
                raise StepFailed(f"找不到客户端启动脚本 {command[1]}",
                                 f"在 MineBot 仓库目录里运行，或设置 {HOME_VAR}=仓库路径，或用 MINEBOT_CLIENT_LAUNCH 指定")
            step.note("ok", f"客户端启动 {shlex.join(command)}")
            java = os.environ.get("JAVA_HOME") or shutil.which("java")
            if java is None and not Path("/usr/lib/jvm").is_dir():
                step.note("warn", "没有找到 Java；客户端需要 Java 25")
        if self.options.create_run_dir:
            try:
                self.run_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StepFailed(f"无法创建会话目录 {self.run_dir}：{exc}") from exc
            step.note("muted", f"会话目录 {self.run_dir}")
        self.environment["MINEBOT_AGENT_LOG_PATH"] = str(self.trace_path)
        step.summary = "全部通过" if all(tone != "warn" for tone, _ in step.lines) else "有提醒"
        if any(tone == "warn" for tone, _ in step.lines):
            step.status = "warn"

    def _step_account(self, step: Step) -> None:
        bot = self.bot
        if not _NAME.fullmatch(bot):
            raise StepFailed(f"机器人名字 {bot!r} 不合法（1–16 位字母、数字、下划线）", "用 --bot 指定，或设置 MINEBOT_REAL_BOT")
        step.summary = bot
        try:
            account = AccountStore(self.options.accounts_dir).ensure_fresh(bot)
        except AccountError as exc:
            raise StepFailed(str(exc), f"运行 minebot login 重新登录，或用离线名字 --bot") from exc
        if account is None or account.kind != "msa":
            self.account_kind = "offline"
            self._client_secrets = {}
            step.note("ok", f"机器人账号 {bot} · 离线登录")
            step.note("muted", "在线模式（正版验证）的服务器需要先 minebot login")
            return
        self.account_kind = "msa"
        self._client_secrets = {"MINEBOT_CLIENT_ACCESS_TOKEN": account.access_token, "MINEBOT_CLIENT_UUID": account.uuid}
        left = max(0.0, account.expires_at - time.time()) / 3600
        step.summary = f"{bot} · 正版"
        step.note("ok", f"机器人账号 {bot} · 正版（微软）· uuid {account.uuid[:8]}…")
        step.note("ok", f"登录有效，{left:.0f} 小时后自动刷新")

    def _step_server(self, step: Step) -> None:
        address = self.server
        try:
            host, _port = split_address(address)
        except PingError as exc:
            raise StepFailed(str(exc), "MINEBOT_CLIENT_SERVER 应为 HOST:PORT") from exc
        if self.disposable and host.lower() not in _LOOPBACK:
            raise StepFailed("可改动世界（disposable）只允许本地测试服", "去掉 --disposable，或改用本地服务器")
        step.summary = address
        try:
            info = ping(address)
        except PingError as exc:
            raise StepFailed(f"{address} 无响应：{exc}", "服务器是否已启动？本地测试服可用 tools/reset-world.sh 启动") from exc
        self.server_info = info
        step.summary = f"{address} · {info['version']}"
        players = f"{info['online']}/{info['max']}" if info.get("online") is not None else "?"
        names = f"（{', '.join(info['names'][:4])}）" if info.get("names") else ""
        step.note("ok", f"{info['version']} · 协议 {info.get('protocol')} · 延迟 {info['latency_ms']}ms")
        step.note("ok", f"在线 {players}{names}")
        if info.get("motd"):
            step.note("muted", f"MOTD {info['motd'][:60]}")
        if self.bot and self.bot in info.get("names", []):
            step.note("warn", f"{self.bot} 已在线；如果不是这台机器上的客户端，进服时会被挤下线")
            step.status = "warn"
        step.note("ok" if self.disposable else "muted",
                  "世界：可改动（disposable）" if self.disposable else "世界：保护模式，改动世界的工具会被拒绝")

    def client_command(self) -> list[str]:
        if self._launch_client_command is not None:
            return list(self._launch_client_command)
        configured = self.environment.get("MINEBOT_CLIENT_LAUNCH") or os.environ.get("MINEBOT_CLIENT_LAUNCH")
        if configured:
            return shlex.split(configured)
        return ["bash", str(self.root / "tools" / "open-client.sh")]

    def _step_client(self, step: Step) -> None:
        host, port = self.ipc
        if port_open(host, port):
            self.client_reused = True
            step.summary = "复用已运行的客户端"
            step.note("ok", f"IPC {host}:{port} 已在监听")
            return
        if not self.options.launch_client:
            raise StepFailed(f"IPC {host}:{port} 没有客户端在监听", "先手动打开客户端，或去掉 --no-client")
        environment = {**self.environment, **self._client_secrets}
        # A single-use Gradle JVM keeps the game inside our process group, so stopping it stops the game.
        environment["GRADLE_OPTS"] = (environment.get("GRADLE_OPTS", "") + " -Dorg.gradle.daemon=false").strip()
        handle = open(self.client_log, "ab")
        try:
            self.client = subprocess.Popen(self.client_command(), cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise StepFailed(f"客户端无法启动：{exc}", "检查 MINEBOT_CLIENT_LAUNCH") from exc
        finally:
            handle.close()
        step.summary = "启动中"
        step.note("ok", f"已启动 pid {self.client.pid} · 日志 {self.client_log.name}")
        deadline = time.monotonic() + self.options.client_timeout_s
        seen: set[str] = set()
        while not port_open(host, port):
            if self._cancel.is_set():
                return
            self._follow_client_log(step, seen)
            if self.client.poll() is not None:
                errors = error_lines(self.client_log)
                raise StepFailed(f"客户端已退出（代码 {self.client.returncode}）" + (f"：{errors[-1]}" if errors else ""),
                                 "按 l 看 client.log")
            if time.monotonic() > deadline:
                raise StepFailed(f"{self.options.client_timeout_s:.0f} 秒内 IPC 没有就绪", "按 l 看 client.log；首次启动要编译和下载，可能更久")
            time.sleep(0.5)
        self._follow_client_log(step, seen)
        step.summary = f"pid {self.client.pid}"
        step.note("ok", f"IPC {host}:{port} 就绪")

    def _follow_client_log(self, step: Step, seen: set[str]) -> None:
        lines = tail_lines(self.client_log, 200)
        self.output.clear()
        self.output.extend(line[-160:] for line in lines[-4:])
        for marker, label in _CLIENT_MILESTONES:
            if marker not in seen and any(marker in line for line in lines):
                seen.add(marker)
                step.note("ok", label)

    def _step_world(self, step: Step) -> None:
        host, port = self.ipc
        ipc = RealClientIpc(host, port, timeout_s=5.0)
        try:
            ipc.connect()
        except OSError as exc:
            raise StepFailed(f"连不上客户端 IPC：{exc}") from exc
        seen: set[str] = set()
        try:
            deadline = time.monotonic() + self.options.world_timeout_s
            hinted = False
            while True:
                if self._cancel.is_set():
                    return
                try:
                    ipc.wait_ready(timeout_s=1.0)
                    break
                except TimeoutError:
                    pass
                except RealClientIpcError as exc:
                    raise StepFailed(f"客户端断开了 IPC：{exc}", "客户端可能崩溃了，按 l 看 client.log") from exc
                if self.client is not None:
                    self._follow_client_log(step, seen)
                waited = self.options.world_timeout_s - (deadline - time.monotonic())
                if not hinted and waited > 20:
                    hinted = True
                    step.note("warn", "还没进入世界；如果角色早已在线，可能另一个 MineBot 会话正连着这个客户端")
                if time.monotonic() > deadline:
                    raise StepFailed(f"{self.options.world_timeout_s:.0f} 秒内没有进入世界",
                                     "看看游戏窗口：是否被服务器拒绝、卡在加载，或被另一个会话占用")
            state = ipc.state()
        finally:
            ipc.close()
        time.sleep(0.3)  # let the client's single-peer accept loop return before the session connects
        self.body = {key: state.get(key) for key in ("pos", "health", "food", "dimension", "gamemode", "disposable_world", "player")}
        pos = state.get("pos")
        where = " ".join(f"{float(value):.0f}" for value in pos) if isinstance(pos, list) and len(pos) == 3 else "?"
        step.summary = f"{state.get('dimension') or '世界'} {where}".replace("minecraft:", "")
        step.note("ok", f"位置 {where} · 生命 {state.get('health', '?')} · 饥饿 {state.get('food', '?')}")
        reported = state.get("disposable_world")
        if reported is not None and bool(reported) != self.disposable:
            step.status = "warn"
            step.note("warn", f"客户端实际为{'可改动' if reported else '保护'}模式，与配置不一致；需要重启客户端才会生效")

    def session_command(self) -> list[str]:
        if self._session_command is not None:
            return list(self._session_command)
        command = [sys.executable, "-m", "minebot.app.real_server_session", "--interactive"]
        if self.options.goal:
            command.append(self.options.goal)
        if self.options.max_steps is not None:
            command += ["--max-steps", str(self.options.max_steps)]
        return command

    def _step_agent(self, step: Step) -> None:
        environment = dict(self.environment)
        environment["PYTHONUNBUFFERED"] = "1"
        handle = open(self.agent_log, "ab")
        try:
            self.session = subprocess.Popen(self.session_command(), cwd=self.root, env=environment, stdin=subprocess.PIPE,
                                            stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise StepFailed(f"会话进程无法启动：{exc}") from exc
        finally:
            handle.close()
        step.summary = f"pid {self.session.pid}"
        step.note("ok", f"会话进程 pid {self.session.pid} · 日志 {self.agent_log.name}")
        follower = TraceFollower(self.trace_path)
        deadline = time.monotonic() + self.options.agent_timeout_s
        labels = {"provider_manifest": "模型就绪", "runtime_scope": "世界身份已确认", "startup_reconciliation": "恢复了上次的任务状态"}
        while True:
            if self._cancel.is_set():
                return
            records, _ = follower.read()
            for record in records:
                event = record.get("event")
                if event in labels:
                    step.note("ok", labels.pop(event))
                if event == "interactive_ready":
                    step.note("ok", "Agent 已就绪，开始接收指令")
                    step.summary = "就绪"
                    return
            self.output.clear()
            self.output.extend(line[-160:] for line in tail_lines(self.agent_log, 4))
            if self.session.poll() is not None:
                last = tail_lines(self.agent_log, 3)
                raise StepFailed(f"会话进程已退出（代码 {self.session.returncode}）" + (f"：{last[-1]}" if last else ""),
                                 "按 l 看 agent.log")
            if time.monotonic() > deadline:
                raise StepFailed(f"{self.options.agent_timeout_s:.0f} 秒内 Agent 没有就绪", "按 l 看 agent.log")
            time.sleep(0.2)

    def watch_client_log(self) -> dict[str, Any]:
        """Server connection as the game client logs it: Minecraft's own disconnect line and the
        mod's ``MINEBOT_PHASE_A ready`` after (re)joining. The trace has no connection events."""
        try:
            with open(self.client_log, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                if size < self._log_offset:  # truncated or replaced
                    self._log_offset = 0
                handle.seek(self._log_offset)
                data = handle.read(1 << 20)
        except OSError:
            return self.connection
        cut = data.rfind(b"\n") + 1
        self._log_offset += cut
        for line in data[:cut].decode("utf-8", "replace").splitlines():
            match = _DISCONNECT.search(line)
            if match:
                self.connection = {"state": "lost", "reason": match.group(1).strip(), "since": time.time()}
            elif "MINEBOT_PHASE_A ready" in line:
                self.connection = {"state": "connected", "reason": "", "since": time.time()}
        return self.connection

    def restart_client(self) -> bool:
        """Start the game client again after it exited, and let the Agent find the world again."""
        if self.client_alive or not self.environment:
            return False
        if self.client is not None and self.client.poll() is not None:
            self.client = None
        self.client_reused = False
        step = self.steps[3]  # phase stays "ready": the Agent session is still running
        step.status, step.lines, step.error, step.hint, step.summary = "running", [], "", "", ""
        step.started, step.ended = time.monotonic(), None
        self.output.clear()
        self._cancel.clear()
        try:
            self._step_client(step)
            if step.status == "running":
                step.status = "ok"
        except StepFailed as failure:
            step.status, step.error, step.hint = "fail", failure.error, failure.hint
            step.ended = time.monotonic()
            self._write_record()
            return False
        step.ended = time.monotonic()
        self.say("ok", "客户端已重新启动，等待 Agent 重新连接")
        self._write_record()
        return True

    # ------------------------------------------------------------ control
    def send(self, line: str) -> bool:
        """Deliver one operator line to the session, exactly as if typed into its terminal."""
        text = line.replace("\n", " ").strip()
        if not text or not self.session_alive() or self.session.stdin is None:
            return False
        with self._stdin_lock:
            try:
                self.session.stdin.write((text + "\n").encode("utf-8"))
                self.session.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return False
        lowered = text.lower()
        if lowered.startswith("/pause"):
            self.agent_paused = True
        elif lowered.startswith(("/continue", "/cancel", "/stop")):
            self.agent_paused = False
        return True

    def say(self, tone: str, text: str) -> None:
        self.messages.append((time.time(), tone, text))

    def shutdown(self, *, stop_client: bool = True, quit_timeout_s: float = 30.0) -> None:
        """Stop the Agent gracefully, then the client this launcher started. Safe to call twice."""
        if self.phase == "stopped":
            return
        self.phase = "stopping"
        self._cancel.set()
        session = self.session
        if session is not None and session.poll() is None:
            if self.send("/quit"):
                self.say("muted", "已发送 /quit，等待 Agent 收尾当前动作")
            if not _wait(session, quit_timeout_s):
                self.say("warn", "收尾超时，发送中断信号")
                _signal_group(session, signal.SIGINT)
                if not _wait(session, 10):
                    _signal_group(session, signal.SIGTERM)
                    if not _wait(session, 5):
                        _signal_group(session, signal.SIGKILL)
                        session.wait()
            self.say("ok", f"Agent 已退出（代码 {session.returncode}）")
        if session is not None and session.stdin is not None:
            try:
                session.stdin.close()
            except OSError:
                pass
        client = self.client
        if client is not None and client.poll() is None:
            if stop_client:
                self.say("muted", "关闭游戏客户端")
                _signal_group(client, signal.SIGTERM)
                if not _wait(client, 15):
                    _signal_group(client, signal.SIGKILL)
                    client.wait()
                self.say("ok", "客户端已关闭")
            else:
                self.say("muted", f"客户端保持运行（pid {client.pid}）")
        elif self.client_reused:
            self.say("muted", "客户端不是本次启动的，保持运行")
        self.phase = "stopped"
        self._write_record()

    def _write_record(self) -> None:
        if not self.run_dir.is_dir():
            return
        record = {
            "phase": self.phase,
            "started_at": datetime.fromtimestamp(self.started_at, timezone.utc).isoformat(),
            "bot": self.bot,
            "account": self.account_kind,
            "profile": self.profile_name or None,
            "server": self.server,
            "server_info": self.server_info,
            "model": self.model,
            "disposable": self.disposable,
            "env_file": str(self.env_path) if self.env_path else None,
            "client_pid": self.client.pid if self.client else None,
            "client_reused": self.client_reused,
            "session_pid": self.session.pid if self.session else None,
            "session_exit": self.session.returncode if self.session else None,
            "launcher_pid": os.getpid(),
            "steps": [{**asdict(step), "elapsed_s": step.elapsed()} for step in self.steps],
        }
        for item in record["steps"]:
            item.pop("started", None)
            item.pop("ended", None)
        try:
            (self.run_dir / "launch.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass


def _wait(process: subprocess.Popen[bytes], timeout_s: float) -> bool:
    try:
        process.wait(timeout=timeout_s)
        return True
    except subprocess.TimeoutExpired:
        return False


def _signal_group(process: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except (ProcessLookupError, PermissionError):
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass


__all__ = ["HOME_VAR", "find_home", "LaunchOptions", "Launcher", "STEPS", "Step", "StepFailed", "default_run_dir", "shell_environment"]
