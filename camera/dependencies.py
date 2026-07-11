from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DependencyArtifact:
    name: str
    version: str
    license: str
    path: Path
    sha256: str
    required: bool = True


@dataclass(frozen=True)
class CameraDependencyConfig:
    expected_mc_version: str
    launcher_command: str
    launcher_profile: Path
    display: str | None
    ffmpeg_command: str
    encoder: str
    output_directory: Path
    artifacts: tuple[DependencyArtifact, ...] = ()


@dataclass(frozen=True)
class DependencyCheck:
    name: str
    ok: bool
    required: bool
    detail: str


@dataclass(frozen=True)
class DependencyReport:
    expected_mc_version: str
    checks: tuple[DependencyCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok or not check.required for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "expected_mc_version": self.expected_mc_version,
            "checks": [asdict(check) for check in self.checks],
        }


class DependencyError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]


def check_dependencies(
    config: CameraDependencyConfig,
    *,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    runner: Runner = subprocess.run,
) -> DependencyReport:
    environment = os.environ if environ is None else environ
    checks: list[DependencyCheck] = []

    java_path = _resolve_command(config_value="java", which=which)
    java_ok, java_detail = _java_check(java_path, runner)
    checks.append(DependencyCheck("java", java_ok, True, java_detail))

    launcher_path = _resolve_command(config.launcher_command, which)
    checks.append(
        DependencyCheck(
            "launcher",
            launcher_path is not None,
            True,
            f"launcher command ready ({Path(launcher_path).name})" if launcher_path else "launcher command not found",
        )
    )

    profile_ok = config.launcher_profile.exists() and os.access(config.launcher_profile, os.R_OK)
    checks.append(
        DependencyCheck(
            "launcher_profile",
            profile_ok,
            True,
            "external launcher profile is readable" if profile_ok else "external launcher profile is missing or unreadable",
        )
    )

    display_ready = bool(config.display or environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY"))
    checks.append(
        DependencyCheck(
            "display",
            display_ready,
            True,
            "capture display is configured" if display_ready else "no capture display is configured",
        )
    )

    ffmpeg_path = _resolve_command(config.ffmpeg_command, which)
    ffmpeg_ok, ffmpeg_detail = _ffmpeg_check(ffmpeg_path, runner)
    checks.append(DependencyCheck("ffmpeg", ffmpeg_ok, True, ffmpeg_detail))

    encoder_ok, encoder_detail = _encoder_check(ffmpeg_path, config.encoder, runner)
    checks.append(DependencyCheck("encoder", encoder_ok, True, encoder_detail))

    checks.append(_output_disk_check(config.output_directory))
    checks.extend(_artifact_check(artifact) for artifact in config.artifacts)

    return DependencyReport(expected_mc_version=config.expected_mc_version, checks=tuple(checks))


def _resolve_command(config_value: str, which: Which) -> str | None:
    candidate = Path(config_value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return which(config_value)


def _java_check(java_path: str | None, runner: Runner) -> tuple[bool, str]:
    if java_path is None:
        return False, "Java command not found"
    result = _run_probe([java_path, "-version"], runner)
    output = f"{result.stdout}\n{result.stderr}" if result is not None else ""
    match = re.search(r'version\s+"(\d+)', output)
    if result is None or result.returncode != 0 or match is None:
        return False, "Java version probe failed"
    major = int(match.group(1))
    if major < 25:
        return False, f"Java {major} is too old; Java 25 or newer is required"
    return True, f"Java {major} ready"


def _ffmpeg_check(ffmpeg_path: str | None, runner: Runner) -> tuple[bool, str]:
    if ffmpeg_path is None:
        return False, "ffmpeg command not found"
    result = _run_probe([ffmpeg_path, "-version"], runner)
    if result is None or result.returncode != 0:
        return False, "ffmpeg version probe failed"
    first_line = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "ffmpeg ready")
    return True, first_line[:160]


def _encoder_check(ffmpeg_path: str | None, encoder: str, runner: Runner) -> tuple[bool, str]:
    if ffmpeg_path is None:
        return False, f"encoder {encoder} cannot be checked without ffmpeg"
    result = _run_probe([ffmpeg_path, "-hide_banner", "-encoders"], runner)
    if result is None or result.returncode != 0:
        return False, "ffmpeg encoder probe failed"
    available = any(encoder in line.split() for line in result.stdout.splitlines())
    return (
        (True, f"encoder {encoder} ready")
        if available
        else (False, f"encoder {encoder} is unavailable")
    )


def _output_disk_check(directory: Path) -> DependencyCheck:
    ready = directory.is_dir() and os.access(directory, os.W_OK)
    if not ready:
        return DependencyCheck("output_disk", False, True, "output directory is missing or not writable")
    usage = shutil.disk_usage(directory)
    free_gib = usage.free / (1024**3)
    return DependencyCheck("output_disk", free_gib >= 2.0, True, f"output disk has {free_gib:.1f} GiB free")


def _artifact_check(artifact: DependencyArtifact) -> DependencyCheck:
    name = f"artifact:{artifact.name}"
    if not artifact.name or not artifact.version or not artifact.license:
        return DependencyCheck(name, False, artifact.required, "artifact provenance metadata is incomplete")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", artifact.sha256):
        return DependencyCheck(name, False, artifact.required, "artifact SHA-256 is malformed")
    if not artifact.path.is_file():
        return DependencyCheck(name, False, artifact.required, "pinned artifact is missing")
    actual = _sha256(artifact.path)
    if actual.lower() != artifact.sha256.lower():
        return DependencyCheck(name, False, artifact.required, "pinned artifact hash mismatch")
    return DependencyCheck(
        name,
        True,
        artifact.required,
        f"{artifact.version} ({artifact.license}) hash verified",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_probe(command: list[str], runner: Runner) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
