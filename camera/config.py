from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from camera.dependencies import CameraDependencyConfig, DependencyArtifact


class CameraConfigError(ValueError):
    pass


_SECRET_KEY_PARTS = ("token", "password", "secret", "credential")
_EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")


def load_dependency_config(path: Path) -> CameraDependencyConfig:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as error:
        raise CameraConfigError("camera config cannot be read") from error
    except tomllib.TOMLDecodeError as error:
        raise CameraConfigError("camera config is invalid TOML") from error

    _reject_embedded_secret_fields(document)
    observer = _table(document, "observer")
    capture = _optional_table(document, "capture")
    output = _table(document, "output")
    record = _table(output, "record")
    dependencies = _optional_table(document, "dependencies")

    expected_version = _string(observer, "expected_mc_version")
    if not _EXACT_VERSION.fullmatch(expected_version):
        raise CameraConfigError("observer.expected_mc_version must be an exact Minecraft version")

    launcher_profile = _resolve(path, _string(observer, "launcher_profile"))
    output_directory = _resolve(path, _string(record, "directory"))
    artifacts = _parse_artifacts(path, dependencies.get("artifacts", []))

    return CameraDependencyConfig(
        expected_mc_version=expected_version,
        launcher_command=_optional_string(observer, "launcher_command", "prismlauncher"),
        launcher_profile=launcher_profile,
        display=_nullable_string(observer, "display"),
        ffmpeg_command=_optional_string(capture, "ffmpeg_command", "ffmpeg"),
        encoder=_optional_string(capture, "encoder", "libx264"),
        output_directory=output_directory,
        artifacts=artifacts,
    )


def _parse_artifacts(config_path: Path, raw: Any) -> tuple[DependencyArtifact, ...]:
    if not isinstance(raw, list):
        raise CameraConfigError("dependencies.artifacts must be an array of tables")
    artifacts: list[DependencyArtifact] = []
    names: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise CameraConfigError("each dependency artifact must be a table")
        name = _string(entry, "name")
        if name in names:
            raise CameraConfigError("dependency artifact names must be unique")
        names.add(name)
        required = entry.get("required", True)
        if not isinstance(required, bool):
            raise CameraConfigError("dependency artifact required must be a boolean")
        artifacts.append(
            DependencyArtifact(
                name=name,
                version=_string(entry, "version"),
                license=_string(entry, "license"),
                path=_resolve(config_path, _string(entry, "path")),
                sha256=_string(entry, "sha256"),
                required=required,
            )
        )
    return tuple(artifacts)


def _reject_embedded_secret_fields(value: Any, prefix: str = "") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower()
            qualified = f"{prefix}.{key}" if prefix else str(key)
            if not normalized.endswith("_env") and any(part in normalized for part in _SECRET_KEY_PARTS):
                raise CameraConfigError(f"secret-like field is forbidden in camera config: {qualified}")
            _reject_embedded_secret_fields(nested, qualified)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_embedded_secret_fields(nested, f"{prefix}[{index}]")


def _table(parent: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise CameraConfigError(f"{name} must be a table")
    return value


def _optional_table(parent: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = parent.get(name, {})
    if not isinstance(value, dict):
        raise CameraConfigError(f"{name} must be a table")
    return value


def _string(parent: Mapping[str, Any], name: str) -> str:
    value = parent.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CameraConfigError(f"{name} must be a nonempty string")
    return value.strip()


def _optional_string(parent: Mapping[str, Any], name: str, fallback: str) -> str:
    if name not in parent:
        return fallback
    return _string(parent, name)


def _nullable_string(parent: Mapping[str, Any], name: str) -> str | None:
    if name not in parent:
        return None
    return _string(parent, name)


def _resolve(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return config_path.parent / candidate
