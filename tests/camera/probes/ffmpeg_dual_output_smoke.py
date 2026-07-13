"""Short local smoke for one ffmpeg encode feeding recording and live output."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from minebot.camera.config import CameraServiceConfig
from minebot.camera.control.follow import FollowConfig
from minebot.camera.output.ffmpeg import build_ffmpeg_command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--port", type=int, default=23811)
    arguments = parser.parse_args()

    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    live_path = arguments.output_dir / "live-pull.mp4"
    publish_url = f"tcp://127.0.0.1:{arguments.port}"
    pull_url = f"tcp://127.0.0.1:{arguments.port}?listen=1"
    config = _smoke_config(arguments.output_dir, arguments.ffmpeg)
    command, _ = build_ffmpeg_command(
        config,
        session_id="dual-output-smoke",
        live_publish_url=publish_url,
    )

    receiver = subprocess.Popen(
        [
            arguments.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            pull_url,
            "-t",
            "3",
            "-c:v",
            "copy",
            str(live_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.4)
        sender = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        if sender.returncode != 0:
            raise RuntimeError(f"dual-output sender failed: {sender.stderr[-1000:]}")
        try:
            _, receiver_stderr = receiver.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            receiver.terminate()
            _, receiver_stderr = receiver.communicate(timeout=3)
            raise RuntimeError(f"live pull did not finish; sender: {sender.stderr[-1000:]}")
        if receiver.returncode != 0:
            detail = receiver_stderr.decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"live pull failed: {detail}")
    finally:
        if receiver.poll() is None:
            receiver.kill()
            receiver.wait(timeout=3)

    recordings = sorted(arguments.output_dir.glob("camera-dual-output-smoke-*.mp4"))
    if len(recordings) != 1:
        raise RuntimeError(f"expected one recording segment, found {len(recordings)}")
    result = {
        "record": _probe_and_decode(recordings[0], arguments.ffmpeg, arguments.ffprobe),
        "live_pull": _probe_and_decode(live_path, arguments.ffmpeg, arguments.ffprobe),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _smoke_config(output_dir: Path, ffmpeg: str) -> CameraServiceConfig:
    return CameraServiceConfig(
        enabled=True,
        target="Bot1",
        observer_id="smoke",
        generation=1,
        bridge_endpoint="ws://127.0.0.1:8766",
        heartbeat_s=2.0,
        startup_timeout_s=5.0,
        shutdown_timeout_s=3.0,
        launcher_command=("true",),
        relay_command=(),
        display=None,
        ffmpeg_command=ffmpeg,
        capture_input_args=(
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=15",
            "-t",
            "4",
        ),
        encoder="libx264",
        preset="ultrafast",
        fps=15,
        follow=FollowConfig(),
        record_enabled=True,
        record_directory=output_dir,
        segment_s=10,
        live_enabled=True,
        live_publish_url_env="SMOKE_UNUSED",
        live_format="mpegts",
        runtime_directory=output_dir / ".runtime",
    )


def _probe_and_decode(path: Path, ffmpeg: str, ffprobe: str) -> dict[str, object]:
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,pix_fmt",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        timeout=10,
        check=True,
    )
    payload = json.loads(probe.stdout)
    stream = payload["streams"][0]
    return {
        "path": str(path),
        "duration_s": float(payload["format"]["duration"]),
        "codec": stream["codec_name"],
        "width": stream["width"],
        "height": stream["height"],
        "pix_fmt": stream["pix_fmt"],
        "full_decode": True,
    }


if __name__ == "__main__":
    raise SystemExit(main())
