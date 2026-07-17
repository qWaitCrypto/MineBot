from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from minebot.camera.config import CameraConfigError, load_camera_config
from minebot.camera.control.observer import ObserverControlClient
from minebot.camera.output.ffmpeg import CameraOutputError, build_ffmpeg_command, resolve_live_publish_url
from minebot.camera.service import (
    _process_start_token,
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


def test_camera_session_waits_for_body_and_stops_owned_sidecar() -> None:
    body = SimpleNamespace(get_state=lambda: SimpleNamespace(missing=True))
    camera = _CameraSession(Path("/tmp/camera.toml"))
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
        patch("minebot.camera.service.stop_service") as stop,
    ):
        camera.maybe_start(body)
        start.assert_not_called()

        body.get_state = lambda: SimpleNamespace(missing=False)
        camera.maybe_start(body)
        camera.maybe_start(body)
        asyncio.run(camera.close())

    start.assert_called_once_with(
        Path("/tmp/camera.toml"),
        force=True,
        wait_for_ready=False,
    )
    stop.assert_called_once_with(Path("/tmp/camera.toml"))


def test_camera_session_does_not_stop_preexisting_camera() -> None:
    camera = _CameraSession(Path("/tmp/camera.toml"))
    body = SimpleNamespace(get_state=lambda: SimpleNamespace(missing=False))
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
        camera.maybe_start(body)
        asyncio.run(camera.close())

    stop.assert_not_called()


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
