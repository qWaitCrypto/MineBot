from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from minebot.camera.config import CameraConfigError, load_camera_config
from minebot.camera.cli.main import main as camera_cli_main
from minebot.camera.control.observer import ObserverControlClient
from minebot.camera.output.ffmpeg import CameraOutputError, build_ffmpeg_command, resolve_live_publish_url
from minebot.camera.service import (
    CameraServiceError,
    _camera_child_environment,
    _maintain_observer,
    _process_alive,
    _process_start_token,
    run_worker,
    _spawn_child,
    _stop_children,
    _supervisor_shutdown_timeout_s,
    _write_state,
    service_status,
    start_service,
)
from minebot.app.real_server_session import _CameraSession, main as real_server_main


def test_runtime_config_is_default_off_and_keeps_commands_as_argv(tmp_path: Path) -> None:
    config = load_camera_config(_camera_config(tmp_path))

    assert config.service.enabled is False
    assert config.service.launcher_command == ("launcher", "--profile", "Camera")
    assert config.service.relay_command == ("mediamtx", "mediamtx.yml")
    assert config.service.capture_input_args[-2:] == ("-i", ":91.0")
    assert config.service.record_enabled is True
    assert config.service.live_enabled is True


def test_camera_child_environment_carries_configured_local_paths(tmp_path: Path) -> None:
    config = load_camera_config(_camera_config(tmp_path))

    environment = _camera_child_environment(config, environ={"UNCHANGED": "yes"})

    assert environment["UNCHANGED"] == "yes"
    assert environment["MINEBOT_CAMERA_PROFILE_DIR"] == str(config.dependencies.launcher_profile)
    assert environment["MINEBOT_CAMERA_RUNTIME_DIR"] == str(config.service.runtime_directory)
    assert environment["MINEBOT_CAMERA_PYTHON"] == sys.executable
    assert environment["MINEBOT_CAMERA_DISPLAY"] == ":91"


def test_camera_cli_explicit_start_uses_the_persistent_config_without_force() -> None:
    config_path = Path("/persistent/camera.toml")
    with (
        patch("minebot.camera.cli.main.resolve_camera_config_path", return_value=config_path) as resolve,
        patch(
            "minebot.camera.cli.main.start_service",
            return_value={"phase": "ready", "target": "Bot1", "recording": True, "live": False},
        ) as start,
    ):
        result = camera_cli_main(["start", "--json"])

    assert result == 0
    resolve.assert_called_once_with(None)
    start.assert_called_once_with(config_path, force=True)


def test_runtime_config_rejects_non_loopback_control_endpoint(tmp_path: Path) -> None:
    path = _camera_config(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("127.0.0.1", "example.com"), encoding="utf-8")

    with pytest.raises(CameraConfigError, match="loopback"):
        load_camera_config(path)


def test_ffmpeg_uses_one_input_and_tee_for_record_and_live(tmp_path: Path) -> None:
    service = load_camera_config(_camera_config(tmp_path)).service
    live_url = resolve_live_publish_url(
        service,
        {"MINEBOT_CAMERA_PUBLISH_URL": "rtsp://127.0.0.1:8554/minebot"},
    )

    command, record_pattern = build_ffmpeg_command(
        service,
        session_id="session-1",
        live_publish_url=live_url,
    )

    assert command.count("-i") == 1
    assert command[-2] == "tee"
    assert "f=segment" in command[-1]
    assert "f=rtsp" in command[-1]
    assert "rtsp://127.0.0.1:8554/minebot" in command[-1]
    assert record_pattern is not None and record_pattern.endswith("camera-session-1-%05d.mp4")


def test_live_publish_url_rejects_embedded_credentials(tmp_path: Path) -> None:
    service = load_camera_config(_camera_config(tmp_path)).service

    with pytest.raises(CameraOutputError, match="must not embed credentials"):
        resolve_live_publish_url(
            service,
            {"MINEBOT_CAMERA_PUBLISH_URL": "rtsp://user:password@127.0.0.1:8554/minebot"},
        )


def test_observer_client_negotiates_generation_and_advances_on_detach(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket = _FakeWebsocket()

    async def connect(*_args, **_kwargs):
        return websocket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))

    async def run() -> None:
        from minebot.camera.control.follow import FollowConfig

        client = await ObserverControlClient.connect(
            "ws://127.0.0.1:8766",
            observer_id="observer",
            generation=7,
            target="Bot1",
            follow=FollowConfig(),
        )
        await client.heartbeat()
        await client.close()

    asyncio.run(run())

    requests = websocket.requests
    assert [request["type"] for request in requests] == ["HELLO", "STATUS", "ATTACH", "HEARTBEAT", "DETACH"]
    mutations = requests[2:]
    assert len({request["lease_id"] for request in mutations}) == 1
    assert [request["generation"] for request in mutations] == [12, 12, 13]
    assert websocket.closed is True


def test_observer_disconnect_closes_transport_without_detach(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket = _FakeWebsocket()

    async def connect(*_args, **_kwargs):
        return websocket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))

    async def run() -> None:
        from minebot.camera.control.follow import FollowConfig

        client = await ObserverControlClient.connect(
            "ws://127.0.0.1:8766",
            observer_id="observer",
            generation=7,
            target="Bot1",
            follow=FollowConfig(),
        )
        await client.disconnect()
        await client.close()

    asyncio.run(run())

    assert [request["type"] for request in websocket.requests] == ["HELLO", "STATUS", "ATTACH"]
    assert websocket.closed is True


def test_observer_reconnect_resumes_existing_lease_without_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = _FakeWebsocket()
    replacement = _FakeWebsocket()
    websockets = iter((initial, replacement))

    async def connect(*_args, **_kwargs):
        return next(websockets)

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))

    async def run() -> None:
        from minebot.camera.control.follow import FollowConfig

        client = await ObserverControlClient.connect(
            "ws://127.0.0.1:8766",
            observer_id="observer",
            generation=7,
            target="Bot1",
            follow=FollowConfig(),
        )
        await client.disconnect()
        resumed = await client.reconnect("ws://127.0.0.1:8766")
        assert resumed is client
        await client.close()

    asyncio.run(run())

    assert [request["type"] for request in initial.requests] == ["HELLO", "STATUS", "ATTACH"]
    assert [request["type"] for request in replacement.requests] == [
        "HELLO",
        "STATUS",
        "HEARTBEAT",
        "DETACH",
    ]
    first_attach = initial.requests[-1]
    resumed_heartbeat = replacement.requests[-2]
    assert resumed_heartbeat["lease_id"] == first_attach["lease_id"]
    assert resumed_heartbeat["generation"] == first_attach["generation"]
    assert replacement.requests[-1]["generation"] == first_attach["generation"] + 1
    assert initial.closed is True
    assert replacement.closed is True


def test_start_is_idempotent_for_a_live_supervisor_state(tmp_path: Path) -> None:
    path = _camera_config(tmp_path)
    service = load_camera_config(path).service
    _write_state(
        service,
        phase="ready",
        pid=os.getpid(),
        process_start=_process_start_token(os.getpid()),
        target="Bot1",
        recording=True,
        live=True,
        children={},
        error=None,
    )

    state = start_service(path, force=True)

    assert state["phase"] == "ready"
    assert state["pid"] == os.getpid()
    assert state["started"] is False


def test_stale_supervisor_state_is_reported_failed(tmp_path: Path) -> None:
    path = _camera_config(tmp_path)
    service = load_camera_config(path).service
    _write_state(
        service,
        phase="ready",
        pid=99_999_999,
        process_start="missing",
        target="Bot1",
        recording=True,
        live=True,
        children={},
        error=None,
    )

    state = service_status(path)

    assert state["phase"] == "failed"
    assert state["error"] == "Camera supervisor is not running"


def test_failed_status_reaps_owned_zombie_without_rewriting_failure(tmp_path: Path) -> None:
    path = _camera_config(tmp_path)
    service = load_camera_config(path).service
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
        start_new_session=True,
    )
    start = _process_start_token(process.pid)
    assert start is not None
    _write_state(
        service,
        phase="failed",
        pid=process.pid,
        process_start=start,
        target="Bot1",
        recording=True,
        live=False,
        children={},
        error="heartbeat_timeout",
    )

    for _ in range(200):
        fields = Path(f"/proc/{process.pid}/stat").read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Camera supervisor fixture did not exit")

    state = service_status(path)

    assert state["phase"] == "failed"
    assert state["error"] == "heartbeat_timeout"
    assert not Path(f"/proc/{process.pid}").exists()
    process.poll()


def test_heartbeat_timeout_disconnects_and_reconnects_without_restarting_children(tmp_path: Path) -> None:
    service = load_camera_config(_camera_config(tmp_path)).service
    replacement = SimpleNamespace()
    observer = SimpleNamespace(
        heartbeat=AsyncMock(side_effect=TimeoutError),
        disconnect=AsyncMock(),
        reconnect=AsyncMock(return_value=replacement),
    )
    child = SimpleNamespace(poll=lambda: None)
    children = {"observer_client": child, "ffmpeg": child}
    reconnecting = []

    async def run() -> tuple[object, bool]:
        with patch(
            "minebot.camera.service._connect_observer",
            new=AsyncMock(return_value=replacement),
        ) as connect:
            result = await _maintain_observer(
                service,
                children,
                asyncio.Event(),
                observer,
                before_reconnect=lambda: reconnecting.append(True),
            )
        connect.assert_not_awaited()
        return result

    maintained, reconnected = asyncio.run(run())

    observer.disconnect.assert_awaited_once()
    observer.reconnect.assert_awaited_once_with(service.bridge_endpoint)
    assert reconnecting == [True]
    assert maintained is replacement
    assert reconnected is True


def test_healthy_heartbeat_keeps_existing_observer_connection(tmp_path: Path) -> None:
    service = load_camera_config(_camera_config(tmp_path)).service
    observer = SimpleNamespace(heartbeat=AsyncMock())
    child = SimpleNamespace(poll=lambda: None)

    async def run() -> tuple[object, bool]:
        with patch("minebot.camera.service._connect_observer", new=AsyncMock()) as connect:
            result = await _maintain_observer(
                service,
                {"observer_client": child, "ffmpeg": child},
                asyncio.Event(),
                observer,
                before_reconnect=lambda: pytest.fail("healthy heartbeat started reconnection"),
            )
        connect.assert_not_awaited()
        return result

    maintained, reconnected = asyncio.run(run())

    observer.heartbeat.assert_awaited_once()
    assert maintained is observer
    assert reconnected is False


def test_heartbeat_reconnect_failure_remains_typed(tmp_path: Path) -> None:
    service = load_camera_config(_camera_config(tmp_path)).service
    observer = SimpleNamespace(
        heartbeat=AsyncMock(side_effect=TimeoutError),
        disconnect=AsyncMock(),
        reconnect=AsyncMock(side_effect=TimeoutError),
    )
    child = SimpleNamespace(poll=lambda: None)

    async def run() -> None:
        with (
            patch(
                "minebot.camera.service._connect_observer",
                new=AsyncMock(side_effect=CameraServiceError("observer bridge unavailable (TimeoutError)")),
            ),
            pytest.raises(CameraServiceError, match="observer bridge unavailable"),
        ):
            await _maintain_observer(
                service,
                {"observer_client": child, "ffmpeg": child},
                asyncio.Event(),
                observer,
                before_reconnect=lambda: None,
            )

    asyncio.run(run())


def test_worker_reconnects_control_channel_without_restarting_capture(tmp_path: Path) -> None:
    service = replace(
        load_camera_config(_camera_config(tmp_path)).service,
        heartbeat_s=0.001,
        relay_command=(),
    )
    replacement = SimpleNamespace(
        heartbeat=AsyncMock(side_effect=KeyboardInterrupt),
        close=AsyncMock(),
    )
    old_observer = SimpleNamespace(
        heartbeat=AsyncMock(side_effect=TimeoutError),
        disconnect=AsyncMock(),
        reconnect=AsyncMock(return_value=replacement),
    )
    observer_process = SimpleNamespace(pid=101, poll=lambda: None)
    ffmpeg_process = SimpleNamespace(pid=102, poll=lambda: None)
    states: list[dict[str, object]] = []

    def capture_state(_service, **fields) -> None:
        states.append(fields)

    async def run() -> int:
        with (
            patch(
                "minebot.camera.service.load_camera_config",
                return_value=SimpleNamespace(
                    service=service,
                    dependencies=SimpleNamespace(launcher_profile=Path("/camera-profile")),
                ),
            ),
            patch(
                "minebot.camera.service._spawn_child",
                side_effect=[observer_process, ffmpeg_process],
            ) as spawn,
            patch(
                "minebot.camera.service._connect_observer",
                new=AsyncMock(return_value=old_observer),
            ) as connect,
            patch("minebot.camera.service.resolve_live_publish_url", return_value=None),
            patch(
                "minebot.camera.service.build_ffmpeg_command",
                return_value=(("ffmpeg", "capture"), "/recordings/camera-%05d.mp4"),
            ),
            patch("minebot.camera.service._write_state", side_effect=capture_state),
            patch("minebot.camera.service._stop_children", new=AsyncMock()) as stop_children,
        ):
            result = await run_worker(Path("/tmp/camera.toml"))
        assert spawn.call_count == 2
        assert connect.await_count == 1
        stop_children.assert_awaited_once()
        return result

    result = asyncio.run(run())

    assert result == 0
    old_observer.disconnect.assert_awaited_once()
    old_observer.reconnect.assert_awaited_once_with(service.bridge_endpoint)
    replacement.close.assert_awaited_once()
    phases = [state["phase"] for state in states]
    assert phases == ["starting", "connecting", "ready", "connecting", "ready", "stopping", "stopped"]
    reconnecting, reconnected = states[3:5]
    assert reconnecting["children"] == {"observer_client": 101, "ffmpeg": 102}
    assert reconnected["children"] == {"observer_client": 101, "ffmpeg": 102}


def test_child_cleanup_terminates_owned_process_group(tmp_path: Path) -> None:
    grandchild_pid_path = tmp_path / "grandchild.pid"
    process = _spawn_child(
        (
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "open(sys.argv[1],'w').write(str(child.pid)); "
                "time.sleep(60)"
            ),
            str(grandchild_pid_path),
        ),
        os.environ,
    )
    for _ in range(100):
        if grandchild_pid_path.exists():
            break
        time.sleep(0.01)
    grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))

    asyncio.run(_stop_children({"child": process}, timeout_s=2.0))

    assert process.poll() is not None
    for _ in range(100):
        if not Path(f"/proc/{grandchild_pid}").exists():
            break
        time.sleep(0.01)
    assert not Path(f"/proc/{grandchild_pid}").exists()


def test_process_alive_reaps_an_exited_owned_supervisor() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
        start_new_session=True,
    )
    start = _process_start_token(process.pid)
    assert start is not None

    for _ in range(200):
        try:
            fields = Path(f"/proc/{process.pid}/stat").read_text(encoding="utf-8").split()
        except FileNotFoundError:
            break
        if len(fields) > 2 and fields[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Camera supervisor fixture did not exit")

    assert _process_alive(process.pid, start) is False
    assert not Path(f"/proc/{process.pid}").exists()
    process.poll()


def test_supervisor_stop_budget_covers_observer_and_all_child_cleanup(tmp_path: Path) -> None:
    service = load_camera_config(_camera_config(tmp_path)).service

    timeout = _supervisor_shutdown_timeout_s(
        service,
        {"children": {"observer_client": 101, "ffmpeg": 102, "relay": 103}},
    )

    assert timeout == service.shutdown_timeout_s + 3.0 + 3.0 + 2.0


def test_real_server_camera_switch_delegates_lifecycle_to_session() -> None:
    fake_config = object()
    with (
        patch("minebot.app.real_server_session.real_server_config_from_env", return_value=fake_config),
        patch("minebot.app.real_server_session.run_real_server_goal", new=AsyncMock(return_value=7)) as run_goal,
    ):
        result = real_server_main(["observe", "--camera", "--camera-config", "/tmp/camera.toml"])

    assert result == 7
    run_goal.assert_awaited_once_with(
        fake_config,
        "observe",
        max_steps=100_000,
        camera_config=Path("/tmp/camera.toml"),
    )


def test_real_server_camera_switch_uses_persistent_config_when_not_overridden() -> None:
    fake_config = object()
    persistent_config = Path("/persistent/camera.toml")
    with (
        patch("minebot.app.real_server_session.real_server_config_from_env", return_value=fake_config),
        patch("minebot.camera.config.resolve_camera_config_path", return_value=persistent_config) as resolve,
        patch("minebot.app.real_server_session.run_real_server_goal", new=AsyncMock(return_value=7)) as run_goal,
    ):
        result = real_server_main(["observe", "--camera"])

    assert result == 7
    resolve.assert_called_once_with(None)
    run_goal.assert_awaited_once_with(
        fake_config,
        "observe",
        max_steps=100_000,
        camera_config=persistent_config,
    )


def test_camera_session_waits_for_body_and_stops_owned_sidecar() -> None:
    body = SimpleNamespace(get_state=lambda: SimpleNamespace(missing=True))
    camera = _CameraSession(Path("/tmp/camera.toml"))

    async def run() -> None:
        camera.maybe_start(body)
        start.assert_not_called()

        body.get_state = lambda: SimpleNamespace(missing=False)
        camera.maybe_start(body)
        camera.maybe_start(body)
        await camera.close()

    with (
        patch(
            "minebot.camera.service.start_service",
            return_value={
                "phase": "ready",
                "target": "Bot1",
                "recording": True,
                "live": True,
                "started": True,
            },
        ) as start,
        patch(
            "minebot.camera.service.stop_service",
            return_value={"phase": "stopped", "children": {}, "error": None},
        ) as stop,
    ):
        asyncio.run(run())

    start.assert_called_once_with(
        Path("/tmp/camera.toml"),
        force=True,
        wait_for_ready=False,
    )
    stop.assert_called_once_with(Path("/tmp/camera.toml"))


def test_camera_session_does_not_stop_preexisting_camera() -> None:
    camera = _CameraSession(Path("/tmp/camera.toml"))
    body = SimpleNamespace(get_state=lambda: SimpleNamespace(missing=False))

    async def run() -> None:
        camera.maybe_start(body)
        await camera.close()

    with (
        patch(
            "minebot.camera.service.start_service",
            return_value={
                "phase": "ready",
                "target": "Bot1",
                "recording": True,
                "live": True,
                "started": False,
            },
        ),
        patch("minebot.camera.service.stop_service") as stop,
    ):
        asyncio.run(run())

    stop.assert_not_called()


def test_camera_session_keeps_monitoring_after_ready() -> None:
    camera = _CameraSession(Path("/tmp/camera.toml"))
    body = SimpleNamespace(get_state=lambda: SimpleNamespace(missing=False))

    async def run() -> None:
        camera.maybe_start(body)
        for _ in range(20):
            if camera.failure is not None:
                break
            await asyncio.sleep(0.01)
        assert camera.failure == "lease_expired"
        await camera.close()

    with (
        patch(
            "minebot.camera.service.start_service",
            return_value={
                "phase": "ready",
                "target": "Bot1",
                "recording": True,
                "live": False,
                "started": True,
            },
        ),
        patch(
            "minebot.camera.service.service_status",
            return_value={
                "phase": "failed",
                "target": "Bot1",
                "recording": True,
                "live": False,
                "error": "lease_expired",
            },
        ),
        patch(
            "minebot.camera.service.stop_service",
            return_value={"phase": "stopped", "children": {}, "error": None},
        ),
    ):
        asyncio.run(run())


class _FakeWebsocket:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.requests.append(json.loads(payload))

    async def recv(self) -> str:
        request = self.requests[-1]
        response: dict[str, object] = {
            "type": f"{request['type']}_ACK",
            "request_id": request["request_id"],
        }
        if request["type"] == "HELLO":
            response["protocol"] = "observer-control/1"
        if request["type"] == "STATUS":
            response["last_generation"] = 11
        return json.dumps(response)

    async def close(self) -> None:
        self.closed = True


def _camera_config(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    recordings = tmp_path / "recordings"
    recordings.mkdir(exist_ok=True)
    path = tmp_path / "camera.toml"
    path.write_text(
        f"""
[camera]
enabled = false
target = "Bot1"

[observer]
id = "observer"
expected_mc_version = "26.1.2"
launcher_command = ["launcher", "--profile", "Camera"]
launcher_profile = "{profile}"
display = ":91"

[bridge]
endpoint = "ws://127.0.0.1:8766"

[capture]
ffmpeg_command = "ffmpeg"
input_args = ["-f", "x11grab", "-i", ":91.0"]
encoder = "libx264"
fps = 30

[output.record]
enabled = true
directory = "{recordings}"
segment_s = 600

[output.live]
enabled = true
publish_url_env = "MINEBOT_CAMERA_PUBLISH_URL"
format = "rtsp"
relay_command = ["mediamtx", "mediamtx.yml"]
""".strip(),
        encoding="utf-8",
    )
    return path
