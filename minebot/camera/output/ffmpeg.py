from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from minebot.camera.config import CameraServiceConfig


class CameraOutputError(RuntimeError):
    pass


_LIVE_FORMATS = {"flv", "matroska", "mpegts", "rtsp"}


def resolve_live_publish_url(
    config: CameraServiceConfig,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    if not config.live_enabled:
        return None
    environ = os.environ if environ is None else environ
    assert config.live_publish_url_env is not None
    value = (environ.get(config.live_publish_url_env) or "").strip()
    if not value:
        raise CameraOutputError(f"live publish URL env {config.live_publish_url_env} is unset")
    parsed = urlsplit(value)
    if not parsed.scheme:
        raise CameraOutputError("live publish URL must include a scheme")
    if parsed.username is not None or parsed.password is not None:
        raise CameraOutputError("live publish URL must not embed credentials")
    _reject_tee_delimiters(value, "live publish URL")
    return value


def build_ffmpeg_command(
    config: CameraServiceConfig,
    *,
    session_id: str,
    live_publish_url: str | None,
) -> tuple[list[str], str | None]:
    if config.live_enabled and live_publish_url is None:
        raise CameraOutputError("live output is enabled but no publish URL was provided")
    if config.live_format not in _LIVE_FORMATS:
        raise CameraOutputError(f"unsupported live output format: {config.live_format}")
    _reject_tee_delimiters(session_id, "session id")

    slaves: list[str] = []
    record_pattern: str | None = None
    if config.record_enabled:
        config.record_directory.mkdir(parents=True, exist_ok=True)
        record_path = config.record_directory / f"camera-{session_id}-%05d.mp4"
        record_pattern = str(record_path)
        _reject_tee_delimiters(record_pattern, "recording path")
        slaves.append(
            "[f=segment:segment_format=mp4:reset_timestamps=1:"
            f"segment_time={config.segment_s}]{record_pattern}"
        )
    if config.live_enabled:
        assert live_publish_url is not None
        options = f"f={config.live_format}:onfail=ignore"
        if config.live_format == "rtsp":
            options += ":rtsp_transport=tcp"
        slaves.append(f"[{options}]{live_publish_url}")
    if not slaves:
        raise CameraOutputError("Camera has no enabled output")

    command = [
        config.ffmpeg_command,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        *config.capture_input_args,
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        config.encoder,
        "-preset",
        config.preset,
        "-r",
        str(config.fps),
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(config.fps * 2),
        "-f",
        "tee",
        "|".join(slaves),
    ]
    return command, record_pattern


def _reject_tee_delimiters(value: str, label: str) -> None:
    if any(character in value for character in "|[]\n\r"):
        raise CameraOutputError(f"{label} contains an unsupported ffmpeg tee delimiter")
