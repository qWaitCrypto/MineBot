from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minebot.camera.config import CameraServiceConfig, load_camera_config
from minebot.camera.dependencies import check_dependencies
from minebot.camera.output.ffmpeg import CameraOutputError, build_ffmpeg_command, resolve_live_publish_url


class CameraServiceError(RuntimeError):
    pass


_RUNNING_PHASES = {"starting", "connecting", "ready", "stopping"}
_OBSERVER_CLOSE_TIMEOUT_S = 3.0
_CHILD_REAP_TIMEOUT_S = 1.0
_SUPERVISOR_EXIT_GRACE_S = 2.0


def start_service(
    config_path: Path,
    *,
    force: bool = False,
    wait_for_ready: bool = True,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    config = load_camera_config(config_path)
    service = config.service
    if not service.enabled and not force:
        raise CameraServiceError("Camera is disabled in config; use --force or the app --camera switch")

    service.record_directory.mkdir(parents=True, exist_ok=True)
    service.runtime_directory.mkdir(parents=True, exist_ok=True)
    existing = service_status(config_path)
    if _state_process_alive(existing):
        if existing.get("phase") in _RUNNING_PHASES:
            if wait_for_ready:
                return _wait_until_ready(config_path, service, started=False)
            return {**existing, "started": False}
        raise CameraServiceError(str(existing.get("error") or "Camera supervisor is still shutting down"))

    report = check_dependencies(config.dependencies)
    if not report.ok:
        failed = ", ".join(check.name for check in report.checks if check.required and not check.ok)
        raise CameraServiceError(f"Camera preflight failed: {failed}")

    with service.log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "minebot.camera.service", "run", "--config", str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    _write_state(
        service,
        phase="starting",
        pid=process.pid,
        process_start=_process_start_token(process.pid),
        target=service.target,
        recording=service.record_enabled,
        live=service.live_enabled,
        error=None,
    )

    if not wait_for_ready:
        return {**service_status(config_path), "started": True}
    return _wait_until_ready(config_path, service, started=True, process=process)


def _wait_until_ready(
    config_path: Path,
    service: CameraServiceConfig,
    *,
    started: bool,
    process: subprocess.Popen[bytes] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + service.startup_timeout_s
    while time.monotonic() < deadline:
        state = service_status(config_path)
        phase = state.get("phase")
        if phase == "ready":
            return {**state, "started": started}
        if phase in {"failed", "stopped"}:
            raise CameraServiceError(str(state.get("error") or "Camera stopped during startup"))
        if process is not None and process.poll() is not None:
            raise CameraServiceError("Camera supervisor exited during startup")
        time.sleep(0.1)

    if started:
        with contextlib.suppress(CameraServiceError):
            stop_service(config_path)
    raise CameraServiceError("Camera startup timed out")


def service_status(config_path: Path) -> dict[str, Any]:
    config = load_camera_config(config_path.expanduser().resolve())
    state = _read_state(config.service)
    if state.get("phase") in _RUNNING_PHASES and not _state_process_alive(state):
        return {
            **state,
            "phase": "failed",
            "error": state.get("error") or "Camera supervisor is not running",
        }
    return state


def stop_service(config_path: Path) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    service = load_camera_config(config_path).service
    state = _read_state(service)
    pid = _state_pid(state)
    if pid is None or not _state_process_alive(state):
        stopped = {**state, "phase": "stopped", "error": None, "children": {}}
        _write_state(service, **_state_fields(stopped))
        return _read_state(service)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + _supervisor_shutdown_timeout_s(service, state)
    while time.monotonic() < deadline:
        if not _process_alive(pid, state.get("process_start")):
            final = _read_state(service)
            if final.get("phase") != "stopped":
                _write_state(service, **_state_fields({**final, "phase": "stopped", "error": None}))
            return _read_state(service)
        time.sleep(0.1)

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    _write_state(
        service,
        **_state_fields({**state, "phase": "stopped", "error": "forced shutdown after timeout", "children": {}}),
    )
    return _read_state(service)


async def run_worker(config_path: Path) -> int:
    config = load_camera_config(config_path.expanduser().resolve())
    service = config.service
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    children: dict[str, subprocess.Popen[bytes]] = {}
    observer: Any | None = None
    failed_reason: str | None = None
    session_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    base_state = {
        "pid": os.getpid(),
        "process_start": _process_start_token(os.getpid()),
        "target": service.target,
        "recording": service.record_enabled,
        "live": service.live_enabled,
        "session_id": session_id,
    }

    try:
        _write_state(service, phase="starting", error=None, children={}, **base_state)
        child_env = os.environ.copy()
        if service.display is not None:
            child_env["DISPLAY"] = service.display

        if service.relay_command:
            children["relay"] = _spawn_child(service.relay_command, child_env)
        children["observer_client"] = _spawn_child(service.launcher_command, child_env)
        _write_state(service, phase="connecting", error=None, children=_child_pids(children), **base_state)

        observer = await _connect_observer(service, children, stop_event)
        if stop_event.is_set():
            return 0

        live_url = resolve_live_publish_url(service)
        ffmpeg_command, record_pattern = build_ffmpeg_command(
            service,
            session_id=session_id,
            live_publish_url=live_url,
        )
        children["ffmpeg"] = _spawn_child(ffmpeg_command, child_env)
        await asyncio.sleep(0.5)
        _raise_for_exited_child(children)
        _write_state(
            service,
            phase="ready",
            error=None,
            children=_child_pids(children),
            record_pattern=record_pattern,
            **base_state,
        )

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=service.heartbeat_s)
            except TimeoutError:
                await observer.heartbeat()
                _raise_for_exited_child(children)
    except BaseException as error:
        if stop_event.is_set() or isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
            stop_event.set()
        else:
            failed_reason = _public_error(error)
            _write_state(
                service,
                phase="failed",
                error=failed_reason,
                children=_child_pids(children),
                **base_state,
            )
    finally:
        _write_state(
            service,
            phase="stopping",
            error=failed_reason,
            children=_child_pids(children),
            **base_state,
        )
        if observer is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(observer.close(), timeout=_OBSERVER_CLOSE_TIMEOUT_S)
        await _stop_children(children, timeout_s=service.shutdown_timeout_s)
        _write_state(
            service,
            phase="stopped" if failed_reason is None else "failed",
            error=failed_reason,
            children={},
            **base_state,
        )
    return 0 if failed_reason is None else 1


async def _connect_observer(
    service: CameraServiceConfig,
    children: Mapping[str, subprocess.Popen[bytes]],
    stop_event: asyncio.Event,
) -> Any:
    from minebot.camera.control.observer import ObserverControlClient

    deadline = time.monotonic() + service.startup_timeout_s
    last_error: BaseException | None = None
    while time.monotonic() < deadline and not stop_event.is_set():
        _raise_for_exited_child(children)
        try:
            return await ObserverControlClient.connect(
                service.bridge_endpoint,
                observer_id=service.observer_id,
                generation=service.generation,
                target=service.target,
                follow=service.follow,
            )
        except Exception as error:
            last_error = error
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.5)
            except TimeoutError:
                pass
    if stop_event.is_set():
        raise CameraServiceError("Camera stopped during observer connection")
    error_name = type(last_error).__name__ if last_error is not None else "unknown"
    raise CameraServiceError(f"observer bridge unavailable ({error_name})")


def _spawn_child(command: Sequence[str], environ: Mapping[str, str]) -> subprocess.Popen[bytes]:
    if not command:
        raise CameraServiceError("Camera child command is empty")
    return subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(environ),
        start_new_session=True,
    )


def _raise_for_exited_child(children: Mapping[str, subprocess.Popen[bytes]]) -> None:
    for name, process in children.items():
        return_code = process.poll()
        if return_code is not None:
            raise CameraServiceError(f"{name} exited with code {return_code}")


async def _stop_children(
    children: Mapping[str, subprocess.Popen[bytes]],
    *,
    timeout_s: float,
) -> None:
    live = [process for process in reversed(tuple(children.values())) if process.poll() is None]
    for process in live:
        _signal_process_group(process, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    for process in live:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _signal_process_group(process, signal.SIGKILL)
    for process in live:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_CHILD_REAP_TIMEOUT_S)


def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, sig)


def _supervisor_shutdown_timeout_s(service: CameraServiceConfig, state: Mapping[str, Any]) -> float:
    children = state.get("children")
    child_count = len(children) if isinstance(children, Mapping) else 0
    return (
        service.shutdown_timeout_s
        + _OBSERVER_CLOSE_TIMEOUT_S
        + max(1, child_count) * _CHILD_REAP_TIMEOUT_S
        + _SUPERVISOR_EXIT_GRACE_S
    )


def _read_state(service: CameraServiceConfig) -> dict[str, Any]:
    try:
        value = json.loads(service.state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "phase": "stopped",
            "pid": None,
            "target": service.target,
            "recording": service.record_enabled,
            "live": service.live_enabled,
            "children": {},
            "error": None,
        }
    except (OSError, json.JSONDecodeError) as error:
        raise CameraServiceError("Camera state file is unreadable") from error
    if not isinstance(value, dict):
        raise CameraServiceError("Camera state file is invalid")
    return value


def _write_state(service: CameraServiceConfig, **fields: Any) -> None:
    service.runtime_directory.mkdir(parents=True, exist_ok=True)
    state = {**fields, "updated_at": datetime.now(UTC).isoformat()}
    temporary = service.state_path.with_name(
        f".{service.state_path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(service.state_path)


def _state_fields(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key != "updated_at"}


def _state_pid(state: Mapping[str, Any]) -> int | None:
    value = state.get("pid")
    return value if isinstance(value, int) and value > 0 else None


def _state_process_alive(state: Mapping[str, Any]) -> bool:
    pid = _state_pid(state)
    return pid is not None and _process_alive(pid, state.get("process_start"))


def _process_alive(pid: int, expected_start: object = None) -> bool:
    start = _process_start_token(pid)
    if start is None:
        return False
    return expected_start is None or str(expected_start) == start


def _process_start_token(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return None
    return fields[21] if len(fields) > 21 else None


def _child_pids(children: Mapping[str, subprocess.Popen[bytes]]) -> dict[str, int]:
    return {name: process.pid for name, process in children.items()}


def _public_error(error: BaseException) -> str:
    if isinstance(error, (CameraOutputError, CameraServiceError)):
        return str(error)
    return f"Camera failed ({type(error).__name__})"


def _worker_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--config", required=True, type=Path)
    arguments = parser.parse_args(argv)
    return asyncio.run(run_worker(arguments.config))


if __name__ == "__main__":
    raise SystemExit(_worker_main())
