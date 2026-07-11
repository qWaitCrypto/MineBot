from __future__ import annotations

import argparse
from pathlib import Path
from collections.abc import Sequence

from camera.cli.check_deps import run_doctor


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minebot-camera")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="validate the real-client Camera environment")
    doctor.add_argument("--config", type=Path, required=True)
    doctor.add_argument("--json", action="store_true", dest="json_output")
    arguments = parser.parse_args(argv)
    if arguments.command == "doctor":
        return run_doctor(arguments.config, json_output=arguments.json_output)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
