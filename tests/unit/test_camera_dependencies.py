from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from minebot.camera.dependencies import (
    CameraDependencyConfig,
    DependencyArtifact,
    check_dependencies,
)


ROOT = Path(__file__).resolve().parents[2]
DEPENDENCY_LOCK = ROOT / "minebot" / "camera" / "dependencies.lock.json"
CLIENT_BUILD = ROOT / "minecraft" / "camera" / "client" / "build.gradle"


class FakeRunner:
    def __call__(
        self,
        command: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[-1] == "-version" and "java" in Path(command[0]).name:
            return subprocess.CompletedProcess(command, 0, "", 'openjdk version "25.0.3" 2026-04-21\n')
        if command[-1] == "-version" and "ffmpeg" in Path(command[0]).name:
            return subprocess.CompletedProcess(command, 0, "ffmpeg version 8.0\n", "")
        if command[-1] == "-encoders":
            return subprocess.CompletedProcess(command, 0, " V....D libx264 H.264\n", "")
        raise AssertionError(f"unexpected probe command: {command}")


def test_real_client_dependency_report_is_typed_and_secret_path_free(tmp_path: Path) -> None:
    profile = tmp_path / "secure" / "accounts" / "observer-profile"
    profile.parent.mkdir(parents=True)
    profile.write_text("not-a-real-token", encoding="utf-8")
    artifact = tmp_path / "mods" / "camera-utils.jar"
    artifact.parent.mkdir()
    artifact.write_bytes(b"pinned-camera-mod")
    expected_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    output = tmp_path / "recordings"
    output.mkdir()
    config = CameraDependencyConfig(
        expected_mc_version="26.1.2",
        launcher_command="prism-launcher",
        launcher_profile=profile,
        display=":91",
        ffmpeg_command="ffmpeg",
        encoder="libx264",
        output_directory=output,
        artifacts=(
            DependencyArtifact(
                name="camera-adapter",
                version="1.1.2+26.1.2",
                license="ARR-binary",
                path=artifact,
                sha256=expected_hash,
            ),
        ),
    )

    report = check_dependencies(
        config,
        environ={},
        which=lambda command: f"/usr/bin/{command}",
        runner=FakeRunner(),
    )

    assert report.ok
    assert report.expected_mc_version == "26.1.2"
    assert {check.name for check in report.checks} >= {
        "java",
        "launcher",
        "launcher_profile",
        "display",
        "ffmpeg",
        "encoder",
        "output_disk",
        "artifact:camera-adapter",
    }
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert str(profile) not in encoded
    assert str(artifact) not in encoded
    assert "not-a-real-token" not in encoded


def test_dependency_report_accumulates_actionable_failures_without_paths(tmp_path: Path) -> None:
    profile = tmp_path / "private-profile"
    artifact = tmp_path / "camera-utils.jar"
    artifact.write_bytes(b"wrong")
    output = tmp_path / "missing" / "recordings"
    config = CameraDependencyConfig(
        expected_mc_version="26.1.2",
        launcher_command="prism-launcher",
        launcher_profile=profile,
        display=None,
        ffmpeg_command="ffmpeg",
        encoder="libx264",
        output_directory=output,
        artifacts=(
            DependencyArtifact(
                name="camera-adapter",
                version="1.1.2+26.1.2",
                license="ARR-binary",
                path=artifact,
                sha256="0" * 64,
            ),
        ),
    )

    report = check_dependencies(
        config,
        environ={},
        which=lambda command: "/usr/bin/java" if command == "java" else None,
        runner=FakeRunner(),
    )

    assert not report.ok
    failed = {check.name: check.detail for check in report.checks if not check.ok}
    assert {"launcher", "launcher_profile", "display", "ffmpeg", "encoder", "output_disk", "artifact:camera-adapter"} <= failed.keys()
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert str(profile) not in encoded
    assert str(artifact) not in encoded


def test_optional_artifact_is_reported_but_does_not_fail_preflight(tmp_path: Path) -> None:
    output = tmp_path / "recordings"
    output.mkdir()
    config = CameraDependencyConfig(
        expected_mc_version="26.1.2",
        launcher_command="prism-launcher",
        launcher_profile=tmp_path,
        display=":91",
        ffmpeg_command="ffmpeg",
        encoder="libx264",
        output_directory=output,
        artifacts=(
            DependencyArtifact(
                name="sodium",
                version="0.9.1+mc26.1.2",
                license="LGPL-3.0-only",
                path=tmp_path / "missing-sodium.jar",
                sha256="0" * 64,
                required=False,
            ),
        ),
    )

    report = check_dependencies(
        config,
        environ={},
        which=lambda command: f"/usr/bin/{command}",
        runner=FakeRunner(),
    )

    sodium = next(check for check in report.checks if check.name == "artifact:sodium")
    assert not sodium.ok
    assert not sodium.required
    assert report.ok


def test_camera_dependency_lock_is_complete_and_matches_selected_build() -> None:
    document = json.loads(DEPENDENCY_LOCK.read_text(encoding="utf-8"))

    assert document["schema_version"] == 1
    assert document["target_minecraft_version"] == "26.1.2"
    artifacts = document["artifacts"]
    assert len({artifact["name"] for artifact in artifacts}) == len(artifacts)

    required_fields = {
        "name",
        "role",
        "status",
        "project_id",
        "version_id",
        "version",
        "source",
        "artifact",
        "license",
        "sha256",
        "dependencies",
    }
    for artifact in artifacts:
        assert required_fields <= artifact.keys()
        assert artifact["source"].startswith("https://")
        assert re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        assert artifact["license"]
        if artifact["status"].startswith("evaluated-"):
            assert artifact.get("rejection")

    by_name = {artifact["name"]: artifact for artifact in artifacts}
    assert {name for name, artifact in by_name.items() if artifact["status"] == "selected"} == {
        "fabric-api",
        "freecam",
        "sodium",
    }
    assert by_name["camera-utils"]["status"] == "evaluated-rejected"

    build = CLIENT_BUILD.read_text(encoding="utf-8")
    assert f'maven.modrinth:freecam:{by_name["freecam"]["version_id"]}' in build
    assert f'maven.modrinth:sodium:{by_name["sodium"]["version_id"]}' in build
    assert f'fabric-api:{by_name["fabric-api"]["version"]}' in build
