"""Panel preferences remembered between runs: ``~/.config/minebot/panel.json``.

Only presentation choices live here (theme, last page, replay speed, whether the help
hint was seen). A broken or unwritable file is ignored; the panel never fails over it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {"theme": None, "page": 0, "speed": 1, "help_hints": 0}


def default_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "minebot" / "panel.json"


def load(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or default_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        return dict(DEFAULTS)
    return {key: data.get(key, value) for key, value in DEFAULTS.items()}


def save(values: dict[str, Any], path: Path | None = None) -> None:
    target = path or default_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".panel-", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({key: values.get(key, value) for key, value in DEFAULTS.items()}, stream)
        os.replace(temporary, target)
    except OSError:
        pass


__all__ = ["DEFAULTS", "default_path", "load", "save"]
