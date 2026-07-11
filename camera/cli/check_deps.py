from __future__ import annotations

import json
from pathlib import Path

from camera.config import CameraConfigError, load_dependency_config
from camera.dependencies import check_dependencies


def run_doctor(config_path: Path, *, json_output: bool) -> int:
    try:
        config = load_dependency_config(config_path)
    except CameraConfigError as error:
        payload = {"ok": False, "error": str(error)}
        _print(payload, json_output)
        return 2

    report = check_dependencies(config)
    payload = report.to_dict()
    _print(payload, json_output)
    return 0 if report.ok else 2


def _print(payload: dict[str, object], json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("Camera preflight: " + ("ready" if payload.get("ok") else "not ready"))
    for check in payload.get("checks", []):
        if not isinstance(check, dict):
            continue
        marker = "ok" if check.get("ok") else ("optional" if not check.get("required") else "fail")
        print(f"[{marker}] {check.get('name')}: {check.get('detail')}")
    if "error" in payload:
        print(f"[fail] config: {payload['error']}")
