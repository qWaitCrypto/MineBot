from __future__ import annotations

import argparse
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path


_AUTOMATION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("pauseOnLostFocus", "false"),
    ("skipMultiplayerWarning", "true"),
    ("joinedFirstServer", "true"),
    ("onboardAccessibility", "false"),
)
_AUTOMATION_OPTION_VALUES = dict(_AUTOMATION_OPTIONS)


def bootstrap_observer_profile(profile_directory: Path) -> Path:
    """Make an isolated observer profile non-interactive without copying another profile."""
    profile_directory.mkdir(parents=True, exist_ok=True)
    options_path = profile_directory / "options.txt"
    try:
        lines = options_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    normalized: list[str] = []
    emitted: set[str] = set()
    for line in lines:
        key, separator, _ = line.partition(":")
        if separator and key in _AUTOMATION_OPTION_VALUES:
            if key not in emitted:
                normalized.append(f"{key}:{_AUTOMATION_OPTION_VALUES[key]}")
                emitted.add(key)
            continue
        normalized.append(line)
    for key, value in _AUTOMATION_OPTIONS:
        if key not in emitted:
            normalized.append(f"{key}:{value}")

    _write_options_atomically(options_path, "\n".join(normalized) + "\n")
    return options_path


def _write_options_atomically(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".options.", dir=path.parent, text=True)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="prepare an isolated MineBot Camera observer profile")
    parser.add_argument("profile_directory", type=Path)
    arguments = parser.parse_args(argv)
    print(bootstrap_observer_profile(arguments.profile_directory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
