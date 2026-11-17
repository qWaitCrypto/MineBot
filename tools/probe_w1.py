#!/usr/bin/env python3
"""Read-only W1 forest fixture probe."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from minebot.game.rcon import RconClient, RconConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe candidate W1 forest centers.")
    parser.add_argument("--center", action="append", required=True, help="candidate center as x,z")
    parser.add_argument("--radius", type=int, default=48)
    parser.add_argument("--min-columns", type=int, default=16)
    parser.add_argument("--min-grounded-columns", type=int, default=12)
    parser.add_argument("--y-min", type=int, default=48)
    parser.add_argument("--y-max", type=int, default=96)
    parser.add_argument("--spawn-y", type=int, default=70)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    centers = [_parse_center(value) for value in args.center]
    report: list[dict[str, object]] = []
    with RconClient(_rcon_config_from_env()) as rcon:
        rcon.command("script load w1_probe global")
        for cx, cz in centers:
            row = _run_probe(rcon, cx, cz, args)
            failed_checks: list[str] = []
            if int(row["columns"]) < int(args.min_columns):
                failed_checks.append("not_enough_log_columns")
            if int(row["grounded_columns"]) < int(args.min_grounded_columns):
                failed_checks.append("not_enough_grounded_columns")
            spawn = row.get("spawn") if isinstance(row.get("spawn"), dict) else {}
            if not spawn.get("safe"):
                failed_checks.append("unsafe_spawn")
            if abs(cx) > 448 or abs(cz) > 448:
                failed_checks.append("outside_governance_margin")
            row["failed_checks"] = failed_checks
            row["pass"] = not failed_checks
            report.append(row)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for row in report:
            status = "PASS" if row["pass"] else "FAIL"
            print(
                f"{status} center=({row['center'][0]},{row['center'][1]}) "
                f"columns={row['columns']} grounded={row['grounded_columns']} "
                f"spawn={row['spawn']} reasons={','.join(row['failed_checks']) or '-'}"
            )
    return 0 if any(row["pass"] for row in report) else 2


def _rcon_config_from_env() -> RconConfig:
    return RconConfig(
        host=os.environ.get("MINEBOT_REAL_RCON_HOST", "127.0.0.1"),
        port=int(os.environ.get("MINEBOT_REAL_RCON_PORT", "25576")),
        password=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"),
        timeout_s=float(os.environ.get("MINEBOT_REAL_RCON_TIMEOUT", "30")),
        reconnect_attempts=2,
    )


def _parse_center(value: str) -> tuple[int, int]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--center must be x,z")
    return int(parts[0]), int(parts[1])


def _run_probe(rcon: RconClient, cx: int, cz: int, args: argparse.Namespace) -> dict[str, object]:
    raw = rcon.command(
        "script in w1_probe run "
        f"w1_probe({cx},{cz},{int(args.radius)},{int(args.y_min)},{int(args.y_max)},{int(args.spawn_y)})"
    )
    match = re.search(r"=\s*(\{.*\})\s*\(", raw, re.S)
    if not match:
        raise RuntimeError(f"w1_probe did not return JSON object: {raw[:500]}")
    return json.loads(match.group(1))


if __name__ == "__main__":
    raise SystemExit(main())
