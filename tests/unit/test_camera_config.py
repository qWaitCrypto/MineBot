from __future__ import annotations

from pathlib import Path

import pytest

from minebot.camera.config import (
    CameraConfigError,
    default_camera_config_path,
    discover_camera_config_path,
    initialize_camera_config,
    load_camera_config,
    load_dependency_config,
    resolve_camera_config_path,
)


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


def test_camera_config_path_uses_xdg_default_and_explicit_overrides(tmp_path: Path) -> None:
    home = tmp_path / "home"
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "MINEBOT_CAMERA_CONFIG": str(tmp_path / "env.toml")}

    assert default_camera_config_path(environ=environment, home=home) == tmp_path / "xdg-config" / "minebot" / "camera.toml"
    assert resolve_camera_config_path(environ=environment, home=home) == tmp_path / "env.toml"
    assert resolve_camera_config_path(tmp_path / "explicit.toml", environ=environment, home=home) == tmp_path / "explicit.toml"


def test_discover_camera_config_is_optional_only_for_the_standard_default(tmp_path: Path) -> None:
    home = tmp_path / "home"

    assert discover_camera_config_path(environ={}, home=home) is None
    with pytest.raises(CameraConfigError, match="MINEBOT_CAMERA_CONFIG"):
        discover_camera_config_path(environ={"MINEBOT_CAMERA_CONFIG": str(tmp_path / "missing.toml")}, home=home)


def test_initialize_camera_config_creates_persistent_secret_free_defaults(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repo"
    config_path = tmp_path / "config" / "camera.toml"

    created = initialize_camera_config(config_path, home=home, repository_root=root)
    repeated = initialize_camera_config(config_path, home=home, repository_root=root)
    config = load_camera_config(config_path)
    text = config_path.read_text(encoding="utf-8")

    assert created.created is True
    assert repeated.created is False
    assert created.path == config_path.resolve()
    assert config.service.enabled is False
    assert config.service.observer_id == "e3be19c1-6923-3226-8108-2df310ddff82"
    assert config.service.launcher_command == (str(root / "tools" / "camera-observer-client.sh"),)
    assert config.dependencies.required_commands == ("Xvfb", "xdpyinfo")
    assert "token" not in text.lower()
