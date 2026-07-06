from __future__ import annotations

import json
from dataclasses import asdict

from camera.dependencies import DependencyError, check_dependencies


def main() -> int:
    try:
        report = check_dependencies()
    except DependencyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, **asdict(report)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
