"""View an existing X11 game window without controlling or relaunching it.

python -m minebot.camera.live_view --display :99
The Windows companion is tools/watch-client.ps1.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from filelock import FileLock, Timeout


def game_window(display: str) -> tuple[str, int, int] | None:
    tree = subprocess.run(["xwininfo", "-display", display, "-root", "-tree"],
                          capture_output=True, text=True, timeout=5)
    windows = re.findall(r'(0x[0-9a-f]+) "Minecraft[^"\n]*"[^\n]*? (\d+)x(\d+)[+-]', tree.stdout)
    if len(windows) > 1:
        raise RuntimeError("More than one Minecraft window; select its dedicated --display")
    return (windows[0][0], int(windows[0][1]), int(windows[0][2])) if windows else None


def serve(display: str, port: int) -> int:
    directory = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "minebot" / f"live-view-{port}"
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "state.json"
    url = f"http://127.0.0.1:{port}/live.mjpg"
    child = None

    def record(**fields):
        state = {"pid": os.getpid(), "display": display, "url": url, "output_size": [1280, 720], **fields}
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state) + "\n")
        temporary.replace(state_path)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with FileLock(str(directory / "service.lock"), timeout=0):
            try:
                while True:
                    window = game_window(display)
                    if window is None:
                        record(phase="waiting_for_game")
                        time.sleep(1)
                        continue
                    window_id, width, height = window
                    command = [
                        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                        "-stats_period", "2", "-progress", str(directory / "progress.txt"),
                        "-f", "x11grab", "-draw_mouse", "0", "-framerate", "15",
                        "-window_id", window_id, "-i", f"{display}.0",
                        # X11 wall-clock timestamps can repeat after a stall.
                        # Number frames so MJPEG always receives increasing PTS.
                        "-an", "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setpts=N/(15*TB)",
                        "-c:v", "mjpeg", "-q:v", "4", "-threads", "2", "-fps_mode", "passthrough",
                        "-f", "mpjpeg", "-listen", "1", url,
                    ]
                    with (directory / "capture.log").open("ab", buffering=0) as log:
                        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                    record(phase="serving", capture_pid=child.pid, window_id=window_id, source_size=[width, height])
                    child.wait()
                    # A closed viewer or replaced game window ends FFmpeg's
                    # single HTTP connection. Rediscover and serve the next one.
                    record(phase="reconnecting", capture_exit=child.returncode)
                    child = None
                    time.sleep(1)
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                record(phase="stopped")
    except Timeout:
        existing = json.loads(state_path.read_text())
        if existing["display"] != display:
            raise RuntimeError("Live-view port already belongs to another display") from None
        print(f"Live view already running: {url}")
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", default=":99")
    parser.add_argument("--port", type=int, default=18990)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    return serve(args.display, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
