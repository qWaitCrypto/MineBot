"""One-command launcher for the managed local MineBot test environment."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from minebot.app.config import AppConfigError, provider_registry_from_env
from minebot.app.model_provider import ProviderConfigError
from minebot.app.real_server_session import RealServerConfigError, real_server_config_from_env
from minebot.camera.config import (
    CameraConfigError,
    discover_camera_config_path,
    load_camera_config,
)
from minebot.camera.dependencies import check_dependencies


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ENV_VAR = "MINEBOT_RUNTIME_ENV"
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_LOCAL_RUNTIME_DEFAULTS = {
    "MINEBOT_REAL_RCON_HOST": "127.0.0.1",
    "MINEBOT_REAL_RCON_PORT": "25576",
    "MINEBOT_REAL_RCON_PASSWORD": "test",
    "MINEBOT_REAL_BOT": "Bot1",
    "MINEBOT_REAL_RCON_TIMEOUT": "30",
    "MINEBOT_AGENT_LANGUAGE": "Chinese",
}
_RUNTIME_ENV_TEMPLATE = """# MineBot local runtime profile. Keep this file private.
MINEBOT_REAL_RCON_HOST=127.0.0.1
MINEBOT_REAL_RCON_PORT=25576
MINEBOT_REAL_RCON_PASSWORD=test
MINEBOT_REAL_BOT=Bot1
MINEBOT_REAL_RCON_TIMEOUT=30
MINEBOT_AGENT_LANGUAGE=Chinese
MINEBOT_LLM_MODEL=
MINEBOT_LLM_KIND=openai_responses
MINEBOT_LLM_BASE_URL=
MINEBOT_LLM_API_KEY=
MINEBOT_LLM_REASONING_EFFORT=xhigh
MINEBOT_LLM_PARALLEL_TOOL_CALLS=false
"""


class LocalLauncherError(RuntimeError):
    """Local profile or launch orchestration is invalid."""


def default_runtime_env_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    resolved_home = Path.home() if home is None else home
    config_home = environment.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else resolved_home / ".config"
    return root / "minebot" / "runtime.env"


def discover_runtime_env_path(
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    repository_root: Path | None = None,
) -> Path | None:
    environment = os.environ if environ is None else environ
    configured = (environment.get(RUNTIME_ENV_VAR) or "").strip()
    if path is not None or configured:
        selected = (path if path is not None else Path(configured)).expanduser()
        if not selected.is_file():
            source = "--env-file" if path is not None else RUNTIME_ENV_VAR
            raise LocalLauncherError(f"runtime env from {source} does not exist: {selected}")
        return selected.resolve()

    root = ROOT if repository_root is None else repository_root
    candidates = [
        default_runtime_env_path(environ=environment, home=home),
        root / "config" / "runtime.env",
        root / ".env",
    ]
    selected = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
    if selected is not None:
        return selected
    config_dir = root / "config"
    profiles = tuple(config_dir.glob("*.env")) if config_dir.is_dir() else ()
    if len(profiles) == 1:
        return profiles[0].resolve()
    if len(profiles) > 1:
        raise LocalLauncherError("multiple runtime env profiles found; select one with --env-file")
    return None


def initialize_runtime_env(
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, bool]:
    environment = os.environ if environ is None else environ
    configured = (environment.get(RUNTIME_ENV_VAR) or "").strip()
    if path is not None:
        selected = path.expanduser()
    elif configured:
        selected = Path(configured).expanduser()
    else:
        selected = default_runtime_env_path(environ=environment, home=home)
    if selected.exists():
        if not selected.is_file():
            raise LocalLauncherError(f"runtime env path is not a file: {selected}")
        return selected.resolve(), False
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(_RUNTIME_ENV_TEMPLATE, encoding="utf-8")
    selected.chmod(0o600)
    return selected.resolve(), True


def load_runtime_environment(
    path: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    current = dict(os.environ if environ is None else environ)
    if path is None:
        return current

    loaded: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LocalLauncherError(f"runtime env cannot be read: {path}") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise LocalLauncherError(f"runtime env line {line_number} must be KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            raise LocalLauncherError(f"runtime env line {line_number} has an invalid key")
        if key in loaded:
            raise LocalLauncherError(f"runtime env line {line_number} repeats {key}")
        try:
            tokens = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise LocalLauncherError(f"runtime env line {line_number} has invalid quoting") from exc
        if len(tokens) > 1:
            raise LocalLauncherError(f"runtime env line {line_number} must contain one value")
        loaded[key] = tokens[0] if tokens else ""

    loaded.update(current)
    return loaded


def preflight_runtime_environment(
    environment: Mapping[str, str],
    *,
    camera: bool,
) -> tuple[object, Path | None]:
    server = real_server_config_from_env(environment)
    provider = provider_registry_from_env(environment)
    try:
        provider.resolve("primary")
    finally:
        asyncio.run(provider.aclose())

    camera_path = None
    if camera:
        camera_path = discover_camera_config_path(environ=environment)
        if camera_path is None:
            raise LocalLauncherError(
                "Camera config is missing; run minebot-camera init or use --no-camera"
            )
        camera_config = load_camera_config(camera_path)
        report = check_dependencies(camera_config.dependencies, environ=environment)
        if not report.ok:
            failures = ", ".join(
                check.name for check in report.checks if check.required and not check.ok
            )
            raise LocalLauncherError(f"Camera preflight failed: {failures}")
    return server, camera_path


def _require_local_reset(host: str) -> None:
    if host.lower() not in _LOOPBACK_HOSTS:
        raise LocalLauncherError(
            f"world reset is restricted to loopback RCON, got host={host!r}; use --no-reset"
        )


def _reset_environment(environment: Mapping[str, str]) -> dict[str, str]:
    result = dict(environment)
    secret_name = result.get("MINEBOT_LLM_API_KEY_ENV")
    if secret_name:
        result.pop(secret_name, None)
    for key in tuple(result):
        if key.startswith(("MINEBOT_LLM_", "OPENAI_", "ANTHROPIC_")):
            result.pop(key, None)
        elif key.startswith("MINEBOT_CAMERA_") and key != "MINEBOT_CAMERA_CONFIG":
            result.pop(key, None)
    return result


def _default_run_dir(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return root / "logs" / "agentic-runtime" / f"local-{stamp}-{os.getpid()}"


def _session_command(arguments: argparse.Namespace) -> list[str]:
    command = [sys.executable, "-m", "minebot.app.real_server_session"]
    if arguments.goal:
        command.append(arguments.goal)
    command.extend(("--max-steps", str(arguments.max_steps)))
    if arguments.interactive:
        command.append("--interactive")
    if arguments.camera:
        command.append("--camera")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="minebot-local",
        description="Reset, preflight, and run the managed local MineBot session.",
    )
    parser.add_argument("goal", nargs="?", help="Optional initial goal; interactive mode stays open.")
    parser.add_argument("--init", action="store_true", help="Create the private runtime profile and exit.")
    parser.add_argument("--env-file", type=Path, help="Runtime KEY=VALUE profile.")
    parser.add_argument("--run-dir", type=Path, help="Reuse an explicit local trace/state directory.")
    parser.add_argument("--max-steps", type=int, default=100_000)
    parser.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--camera", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--interactive", action=argparse.BooleanOptionalAction, default=True)
    arguments = parser.parse_args(argv)
    if arguments.init:
        try:
            profile, created = initialize_runtime_env(arguments.env_file)
        except LocalLauncherError as exc:
            print(f"MineBot local init failed: {exc}", file=sys.stderr)
            return 2
        state = "created" if created else "already exists"
        print(f"MineBot runtime profile {state}: {profile}")
        return 0
    if not arguments.interactive and not arguments.goal:
        parser.error("goal is required with --no-interactive")

    env_path: Path | None = None
    try:
        env_path = discover_runtime_env_path(arguments.env_file)
        environment = load_runtime_environment(env_path)
        for key, value in _LOCAL_RUNTIME_DEFAULTS.items():
            environment.setdefault(key, value)
        run_dir = (arguments.run_dir or _default_run_dir(ROOT)).expanduser().resolve()
        environment["MINEBOT_AGENT_LOG_PATH"] = str(run_dir / "trace.jsonl")
        environment["MINEBOT_AGENT_STATE_DB"] = str(run_dir / "state.sqlite3")
        server, camera_path = preflight_runtime_environment(
            environment,
            camera=arguments.camera,
        )
        if arguments.reset:
            _require_local_reset(server.rcon.host)
        run_dir.mkdir(parents=True, exist_ok=True)
    except (
        AppConfigError,
        CameraConfigError,
        LocalLauncherError,
        ProviderConfigError,
        RealServerConfigError,
        ValueError,
    ) as exc:
        hint = default_runtime_env_path()
        print(f"MineBot local preflight failed: {exc}", file=sys.stderr)
        if env_path is None:
            print(f"Runtime profile: create {hint} or pass --env-file", file=sys.stderr)
        return 2

    profile_label = str(env_path) if env_path is not None else "process environment"
    print(
        "MineBot local:"
        f" profile={profile_label}"
        f" run_dir={run_dir}"
        f" reset={'on' if arguments.reset else 'off'}"
        f" camera={'on' if camera_path is not None else 'off'}",
        flush=True,
    )
    if arguments.reset:
        reset = subprocess.run(
            [str(ROOT / "tools" / "reset-world.sh")],
            cwd=ROOT,
            env=_reset_environment(environment),
            check=False,
        )
        if reset.returncode != 0:
            return reset.returncode
    session = subprocess.run(
        _session_command(arguments),
        cwd=ROOT,
        env=environment,
        check=False,
    )
    return session.returncode


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LocalLauncherError",
    "default_runtime_env_path",
    "discover_runtime_env_path",
    "initialize_runtime_env",
    "load_runtime_environment",
    "main",
    "preflight_runtime_environment",
]
