from __future__ import annotations

from pathlib import Path

import pytest

from minebot.camera.config import CameraConfigError, load_dependency_config


def test_dependency_config_loads_reference_camera_shape(tmp_path: Path) -> None:
    config_path = tmp_path / "camera.toml"
    config_path.write_text(
        """
[observer]
launcher_command = "prismlauncher"
launcher_profile = "external/profile"
display = ":91"
expected_mc_version = "26.1.2"

[capture]
encoder = "libx264"

[output.record]
directory = "recordings"

[[dependencies.artifacts]]
name = "camera-utils"
version = "1.1.2+26.1.2"
license = "ARR-binary"
path = "mods/camera-utils.jar"
sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[output.live]
publish_url_env = "MINEBOT_CAMERA_PUBLISH_URL"
""".strip(),
        encoding="utf-8",
    )

    config = load_dependency_config(config_path)

    assert config.expected_mc_version == "26.1.2"
    assert config.launcher_command == "prismlauncher"
    assert config.launcher_profile == tmp_path / "external/profile"
    assert config.output_directory == tmp_path / "recordings"
    assert config.artifacts[0].path == tmp_path / "mods/camera-utils.jar"


@pytest.mark.parametrize("field", ["token", "password", "client_secret", "credential"])
def test_dependency_config_rejects_embedded_secret_fields(tmp_path: Path, field: str) -> None:
    config_path = tmp_path / "camera.toml"
    config_path.write_text(
        f"""
[observer]
launcher_profile = "profile"
expected_mc_version = "26.1.2"
{field} = "must-not-be-here"

[output.record]
directory = "recordings"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(CameraConfigError, match="secret-like field"):
        load_dependency_config(config_path)


def test_dependency_config_requires_exact_target_version(tmp_path: Path) -> None:
    config_path = tmp_path / "camera.toml"
    config_path.write_text(
        """
[observer]
launcher_profile = "profile"
expected_mc_version = "latest"

[output.record]
directory = "recordings"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(CameraConfigError, match="exact Minecraft version"):
        load_dependency_config(config_path)
