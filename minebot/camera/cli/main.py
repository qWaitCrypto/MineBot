from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections.abc import Sequence

from minebot.camera.cli.check_deps import run_doctor
from minebot.camera.config import CameraConfigError, initialize_camera_config, resolve_camera_config_path
from minebot.camera.service import CameraServiceError, service_status, start_service, stop_service


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minebot-camera")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create the persistent local Camera configuration")
    initialize.add_argument("--config", type=Path)
    initialize.add_argument("--overwrite", action="store_true")
    initialize.add_argument("--json", action="store_true", dest="json_output")
    doctor = commands.add_parser("doctor", help="validate the real-client Camera environment")
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--json", action="store_true", dest="json_output")
    start = commands.add_parser("start", help="start the optional Camera sidecar")
    start.add_argument("--config", type=Path)
    start.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    start.add_argument("--json", action="store_true", dest="json_output")
    status = commands.add_parser("status", help="show Camera sidecar status")
    status.add_argument("--config", type=Path)
    status.add_argument("--json", action="store_true", dest="json_output")
    stop = commands.add_parser("stop", help="stop Camera and all owned processes")
    stop.add_argument("--config", type=Path)
    stop.add_argument("--json", action="store_true", dest="json_output")
    arguments = parser.parse_args(argv)
    if arguments.command == "init":
        try:
            result = initialize_camera_config(arguments.config, overwrite=arguments.overwrite)
        except CameraConfigError as error:
            _print_error(error, json_output=arguments.json_output)
            return 2
        payload = {"ok": True, "config": str(result.path), "created": result.created}
        if arguments.json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        elif result.created:
            print(f"Camera config initialized: {result.path}")
        else:
            print(f"Camera config already exists: {result.path}")
        return 0

    config_path = resolve_camera_config_path(arguments.config)
    if arguments.command == "doctor":
        return run_doctor(config_path, json_output=arguments.json_output)
    try:
        if arguments.command == "start":
            state = start_service(config_path, force=True)
        elif arguments.command == "status":
            state = service_status(config_path)
        elif arguments.command == "stop":
            state = stop_service(config_path)
        else:
            parser.error("unknown command")
            return 2
    except (CameraConfigError, CameraServiceError) as error:
        _print_error(error, json_output=arguments.json_output)
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


def _print_error(error: Exception, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
    else:
        print(f"Camera error: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
