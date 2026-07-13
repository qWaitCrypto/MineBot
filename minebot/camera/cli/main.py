from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections.abc import Sequence

from minebot.camera.cli.check_deps import run_doctor
from minebot.camera.config import CameraConfigError
from minebot.camera.service import CameraServiceError, service_status, start_service, stop_service


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minebot-camera")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="validate the real-client Camera environment")
    doctor.add_argument("--config", type=Path, required=True)
    doctor.add_argument("--json", action="store_true", dest="json_output")
    start = commands.add_parser("start", help="start the optional Camera sidecar")
    start.add_argument("--config", type=Path, required=True)
    start.add_argument("--force", action="store_true", help="start even when camera.enabled is false")
    start.add_argument("--json", action="store_true", dest="json_output")
    status = commands.add_parser("status", help="show Camera sidecar status")
    status.add_argument("--config", type=Path, required=True)
    status.add_argument("--json", action="store_true", dest="json_output")
    stop = commands.add_parser("stop", help="stop Camera and all owned processes")
    stop.add_argument("--config", type=Path, required=True)
    stop.add_argument("--json", action="store_true", dest="json_output")
    arguments = parser.parse_args(argv)
    if arguments.command == "doctor":
        return run_doctor(arguments.config, json_output=arguments.json_output)
    try:
        if arguments.command == "start":
            state = start_service(arguments.config, force=arguments.force)
        elif arguments.command == "status":
            state = service_status(arguments.config)
        elif arguments.command == "stop":
            state = stop_service(arguments.config)
        else:
            parser.error("unknown command")
            return 2
    except (CameraConfigError, CameraServiceError) as error:
        if arguments.json_output:
            print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        else:
            print(f"Camera error: {error}", file=sys.stderr)
        return 2
    _print_state(state, json_output=arguments.json_output)
    return 0


def _print_state(state: dict[str, object], *, json_output: bool) -> None:
    payload = {"ok": state.get("phase") not in {"failed"}, **state}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        "Camera:"
        f" {payload.get('phase')}"
        f" target={payload.get('target')}"
        f" record={'on' if payload.get('recording') else 'off'}"
        f" live={'on' if payload.get('live') else 'off'}"
    )
    if payload.get("error"):
        print(f"Camera detail: {payload['error']}")


if __name__ == "__main__":
    raise SystemExit(main())
