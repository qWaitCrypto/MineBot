from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from minebot.camera.control.follow import FollowConfig
from minebot.camera.dependencies import CameraDependencyConfig, DependencyArtifact


class CameraConfigError(ValueError):
    pass


_SECRET_KEY_PARTS = ("token", "password", "secret", "credential")
_EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_CAMERA_CONFIG_ENV = "MINEBOT_CAMERA_CONFIG"
_DEFAULT_CAMERA_OBSERVER_ID = "e3be19c1-6923-3226-8108-2df310ddff82"


@dataclass(frozen=True)
class CameraConfigBootstrap:
    path: Path
    created: bool


@dataclass(frozen=True)
class CameraServiceConfig:
    enabled: bool
    target: str
    observer_id: str
    generation: int
    bridge_endpoint: str
    heartbeat_s: float
    startup_timeout_s: float
    shutdown_timeout_s: float
    launcher_command: tuple[str, ...]
    relay_command: tuple[str, ...]
    display: str | None
    ffmpeg_command: str
    capture_input_args: tuple[str, ...]
    encoder: str
    preset: str
    fps: int
    follow: FollowConfig
    record_enabled: bool
    record_directory: Path
    segment_s: int
    live_enabled: bool
    live_publish_url_env: str | None
    live_format: str
    runtime_directory: Path

    @property
    def state_path(self) -> Path:
        return self.runtime_directory / "state.json"

    @property
    def log_path(self) -> Path:
        return self.runtime_directory / "camera.log"


@dataclass(frozen=True)
class CameraConfig:
    dependencies: CameraDependencyConfig
    service: CameraServiceConfig


def default_camera_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    resolved_home = (Path.home() if home is None else home).expanduser()
    config_home = environment.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else resolved_home / ".config"
    return root / "minebot" / "camera.toml"


def resolve_camera_config_path(
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    if path is not None:
        return path.expanduser()
    environment = os.environ if environ is None else environ
    configured = (environment.get(_CAMERA_CONFIG_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    return default_camera_config_path(environ=environment, home=home)


def discover_camera_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    environment = os.environ if environ is None else environ
    configured = (environment.get(_CAMERA_CONFIG_ENV) or "").strip()
    path = resolve_camera_config_path(environ=environment, home=home)
    if path.is_file():
        return path.resolve()
    if configured:
        raise CameraConfigError(f"camera config from {_CAMERA_CONFIG_ENV} does not exist: {path}")
    return None


def initialize_camera_config(
    path: Path | None = None,
    *,
    overwrite: bool = False,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    repository_root: Path | None = None,
) -> CameraConfigBootstrap:
    environment = os.environ if environ is None else environ
    resolved_home = (Path.home() if home is None else home).expanduser()
    config_path = resolve_camera_config_path(path, environ=environment, home=resolved_home)
    if config_path.exists() and not config_path.is_file():
        raise CameraConfigError(f"camera config path is not a file: {config_path}")
    if config_path.is_file() and not overwrite:
        return CameraConfigBootstrap(path=config_path.resolve(), created=False)

    data_root = _camera_data_root(environment, resolved_home)
    state_root = _camera_state_root(environment, resolved_home)
    profile_directory = data_root / "profile"
    recording_directory = data_root / "recordings"
    runtime_directory = state_root / "runtime"
    for directory in (config_path.parent, profile_directory, recording_directory, runtime_directory):
        directory.mkdir(parents=True, exist_ok=True)

    root = (Path(__file__).resolve().parents[2] if repository_root is None else repository_root).resolve()
    launcher = root / "tools" / "camera-observer-client.sh"
    display = (environment.get("MINEBOT_CAMERA_DISPLAY") or ":91").strip() or ":91"
    config_path.write_text(
        _default_camera_config_document(
            launcher=launcher,
            profile_directory=profile_directory,
            recording_directory=recording_directory,
            runtime_directory=runtime_directory,
            display=display,
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return CameraConfigBootstrap(path=config_path.resolve(), created=True)


def load_dependency_config(path: Path) -> CameraDependencyConfig:
    document = _read_document(path)
    return _dependency_config(path, document)


def load_camera_config(path: Path) -> CameraConfig:
    document = _read_document(path)
    dependencies = _dependency_config(path, document)
    camera = _optional_table(document, "camera")
    follow = _optional_table(camera, "follow")
    observer = _table(document, "observer")
    bridge = _table(document, "bridge")
    capture = _table(document, "capture")
    output = _table(document, "output")
    record = _table(output, "record")
    live = _table(output, "live")

    endpoint = _string(bridge, "endpoint")
    _require_loopback_websocket(endpoint)
    enabled = _boolean(camera, "enabled", False)
    record_enabled = _boolean(record, "enabled", True)
    live_enabled = _boolean(live, "enabled", True)
    if not record_enabled and not live_enabled:
        raise CameraConfigError("at least one Camera output must be enabled")

    publish_url_env = _optional_nonempty_string(live, "publish_url_env")
    if live_enabled and publish_url_env is None:
        raise CameraConfigError("output.live.publish_url_env is required when live output is enabled")

    runtime_value = _optional_nonempty_string(camera, "runtime_directory")
    runtime_directory = (
        _resolve(path, runtime_value)
        if runtime_value is not None
        else dependencies.output_directory / ".runtime"
    )
    service = CameraServiceConfig(
        enabled=enabled,
        target=_optional_string(camera, "target", "Bot1"),
        observer_id=_string(observer, "id"),
        generation=_positive_int(observer, "generation", 1),
        bridge_endpoint=endpoint,
        heartbeat_s=_positive_float(bridge, "heartbeat_s", 2.0),
        startup_timeout_s=_positive_float(camera, "startup_timeout_s", 45.0),
        shutdown_timeout_s=_positive_float(camera, "shutdown_timeout_s", 10.0),
        launcher_command=_command(observer, "launcher_command"),
        relay_command=_command(live, "relay_command", required=False),
        display=dependencies.display,
        ffmpeg_command=dependencies.ffmpeg_command,
        capture_input_args=_command(capture, "input_args"),
        encoder=dependencies.encoder,
        preset=_optional_string(capture, "preset", "veryfast"),
        fps=_positive_int(capture, "fps", 30),
        follow=FollowConfig(
            distance=_number(follow, "distance", 5.0),
            azimuth_deg=_number(follow, "azimuth_deg", 180.0),
            elevation_deg=_number(follow, "elevation_deg", 25.0),
            height_offset=_number(follow, "height_offset", 1.6),
            stiffness=_number(follow, "stiffness", 0.2),
            fov_deg=_number(follow, "fov_deg", 70.0),
            collision_margin=_number(follow, "collision_margin", 0.25),
        ),
        record_enabled=record_enabled,
        record_directory=dependencies.output_directory,
        segment_s=_positive_int(record, "segment_s", 600),
        live_enabled=live_enabled,
        live_publish_url_env=publish_url_env,
        live_format=_optional_string(live, "format", "rtsp"),
        runtime_directory=runtime_directory,
    )
    return CameraConfig(dependencies=dependencies, service=service)


def _read_document(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as error:
        raise CameraConfigError("camera config cannot be read") from error
    except tomllib.TOMLDecodeError as error:
        raise CameraConfigError("camera config is invalid TOML") from error

    _reject_embedded_secret_fields(document)
    return document


def _dependency_config(path: Path, document: Mapping[str, Any]) -> CameraDependencyConfig:
    observer = _table(document, "observer")
    capture = _optional_table(document, "capture")
    output = _table(document, "output")
    record = _table(output, "record")
    dependencies = _optional_table(document, "dependencies")

    expected_version = _string(observer, "expected_mc_version")
    if not _EXACT_VERSION.fullmatch(expected_version):
        raise CameraConfigError("observer.expected_mc_version must be an exact Minecraft version")

    launcher_profile = _resolve(path, _string(observer, "launcher_profile"))
    output_directory = _resolve(path, _string(record, "directory"))
    artifacts = _parse_artifacts(path, dependencies.get("artifacts", []))
    required_commands = _string_list(dependencies, "required_commands")

    return CameraDependencyConfig(
        expected_mc_version=expected_version,
        launcher_command=_command(observer, "launcher_command", fallback=("prismlauncher",))[0],
        launcher_profile=launcher_profile,
        display=_nullable_string(observer, "display"),
        ffmpeg_command=_optional_string(capture, "ffmpeg_command", "ffmpeg"),
        encoder=_optional_string(capture, "encoder", "libx264"),
        output_directory=output_directory,
        artifacts=artifacts,
        required_commands=required_commands,
    )


def _parse_artifacts(config_path: Path, raw: Any) -> tuple[DependencyArtifact, ...]:
    if not isinstance(raw, list):
        raise CameraConfigError("dependencies.artifacts must be an array of tables")
    artifacts: list[DependencyArtifact] = []
    names: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise CameraConfigError("each dependency artifact must be a table")
        name = _string(entry, "name")
        if name in names:
            raise CameraConfigError("dependency artifact names must be unique")
        names.add(name)
        required = entry.get("required", True)
        if not isinstance(required, bool):
            raise CameraConfigError("dependency artifact required must be a boolean")
        artifacts.append(
            DependencyArtifact(
                name=name,
                version=_string(entry, "version"),
                license=_string(entry, "license"),
                path=_resolve(config_path, _string(entry, "path")),
                sha256=_string(entry, "sha256"),
                required=required,
            )
        )
    return tuple(artifacts)


def _string_list(parent: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = parent.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise CameraConfigError(f"{name} must be an array of nonempty strings")
    return tuple(item.strip() for item in value)


def _reject_embedded_secret_fields(value: Any, prefix: str = "") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower()
            qualified = f"{prefix}.{key}" if prefix else str(key)
            if not normalized.endswith("_env") and any(part in normalized for part in _SECRET_KEY_PARTS):
                raise CameraConfigError(f"secret-like field is forbidden in camera config: {qualified}")
            _reject_embedded_secret_fields(nested, qualified)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_embedded_secret_fields(nested, f"{prefix}[{index}]")


def _table(parent: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise CameraConfigError(f"{name} must be a table")
    return value


def _optional_table(parent: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = parent.get(name, {})
    if not isinstance(value, dict):
        raise CameraConfigError(f"{name} must be a table")
    return value


def _string(parent: Mapping[str, Any], name: str) -> str:
    value = parent.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CameraConfigError(f"{name} must be a nonempty string")
    return value.strip()


def _optional_string(parent: Mapping[str, Any], name: str, fallback: str) -> str:
    if name not in parent:
        return fallback
    return _string(parent, name)


def _optional_nonempty_string(parent: Mapping[str, Any], name: str) -> str | None:
    if name not in parent:
        return None
    return _string(parent, name)


def _nullable_string(parent: Mapping[str, Any], name: str) -> str | None:
    if name not in parent:
        return None
    return _string(parent, name)


def _boolean(parent: Mapping[str, Any], name: str, fallback: bool) -> bool:
    value = parent.get(name, fallback)
    if not isinstance(value, bool):
        raise CameraConfigError(f"{name} must be a boolean")
    return value


def _positive_int(parent: Mapping[str, Any], name: str, fallback: int) -> int:
    value = parent.get(name, fallback)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CameraConfigError(f"{name} must be a positive integer")
    return value


def _positive_float(parent: Mapping[str, Any], name: str, fallback: float) -> float:
    value = _number(parent, name, fallback)
    if value <= 0:
        raise CameraConfigError(f"{name} must be positive")
    return value


def _number(parent: Mapping[str, Any], name: str, fallback: float) -> float:
    value = parent.get(name, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CameraConfigError(f"{name} must be a number")
    return float(value)


def _command(
    parent: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
    fallback: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = parent.get(name)
    if value is None:
        if required and not fallback:
            raise CameraConfigError(f"{name} must be a command string or array")
        return fallback
    if isinstance(value, str):
        values = (value.strip(),)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = tuple(item.strip() for item in value)
    else:
        raise CameraConfigError(f"{name} must be a command string or array")
    if not values or any(not item for item in values):
        raise CameraConfigError(f"{name} must not contain empty arguments")
    return values


def _require_loopback_websocket(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in _LOOPBACK_HOSTS:
        raise CameraConfigError("bridge.endpoint must be a loopback ws:// or wss:// URL")


def _resolve(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return config_path.parent / candidate


def _camera_data_root(environ: Mapping[str, str], home: Path) -> Path:
    configured = environ.get("XDG_DATA_HOME")
    root = Path(configured).expanduser() if configured else home / ".local" / "share"
    return root / "minebot" / "camera"


def _camera_state_root(environ: Mapping[str, str], home: Path) -> Path:
    configured = environ.get("XDG_STATE_HOME")
    root = Path(configured).expanduser() if configured else home / ".local" / "state"
    return root / "minebot" / "camera"


def _default_camera_config_document(
    *,
    launcher: Path,
    profile_directory: Path,
    recording_directory: Path,
    runtime_directory: Path,
    display: str,
) -> str:
    quote = lambda value: json.dumps(str(value))
    return f'''[camera]
enabled = false
target = "Bot1"
startup_timeout_s = 240
shutdown_timeout_s = 20
runtime_directory = {quote(runtime_directory)}

[camera.follow]
distance = 6.0
azimuth_deg = 155.0
elevation_deg = 25.0
height_offset = 1.6
stiffness = 0.2
fov_deg = 75.0
collision_margin = 0.25

[observer]
id = "{_DEFAULT_CAMERA_OBSERVER_ID}"
generation = 1
expected_mc_version = "26.1.2"
launcher_command = [{quote(launcher)}]
launcher_profile = {quote(profile_directory)}
display = {quote(display)}

[bridge]
endpoint = "ws://127.0.0.1:8766"
heartbeat_s = 2

[capture]
ffmpeg_command = "ffmpeg"
input_args = [
  "-f", "x11grab",
  "-draw_mouse", "0",
  "-framerate", "30",
  "-video_size", "1280x720",
  "-i", {quote(f"{display}.0")},
]
fps = 30
encoder = "libx264"
preset = "veryfast"

[output.record]
enabled = true
directory = {quote(recording_directory)}
segment_s = 600

[output.live]
enabled = false
format = "rtsp"

[dependencies]
required_commands = ["Xvfb", "xdpyinfo"]
'''
