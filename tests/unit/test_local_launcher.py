from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from minebot.app.local_launcher import (
    LocalLauncherError,
    _require_local_reset,
    discover_runtime_env_path,
    initialize_runtime_env,
    load_runtime_environment,
    main,
)


def test_runtime_env_is_data_not_shell_and_process_env_wins(tmp_path: Path) -> None:
    profile = tmp_path / "runtime.env"
    profile.write_text(
        "# local profile\n"
        "export MINEBOT_REAL_RCON_HOST=127.0.0.1\n"
        "MINEBOT_LLM_API_KEY='file-secret'\n"
        "LITERAL='$(touch should-not-run)'\n",
        encoding="utf-8",
    )

    environment = load_runtime_environment(
        profile,
        environ={"MINEBOT_LLM_API_KEY": "process-secret"},
    )

    assert environment["MINEBOT_REAL_RCON_HOST"] == "127.0.0.1"
    assert environment["MINEBOT_LLM_API_KEY"] == "process-secret"
    assert environment["LITERAL"] == "$(touch should-not-run)"
    assert not (tmp_path / "should-not-run").exists()


def test_runtime_env_rejects_multiple_unquoted_tokens(tmp_path: Path) -> None:
    profile = tmp_path / "runtime.env"
    profile.write_text("VALUE=two words\n", encoding="utf-8")

    with pytest.raises(LocalLauncherError, match="one value"):
        load_runtime_environment(profile, environ={})


def test_runtime_env_discovery_prefers_xdg_then_repo(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    repo_profile = repository / "config" / "runtime.env"
    repo_profile.parent.mkdir(parents=True)
    repo_profile.write_text("VALUE=repo\n", encoding="utf-8")

    assert discover_runtime_env_path(
        environ={},
        home=home,
        repository_root=repository,
    ) == repo_profile.resolve()

    xdg_profile = home / ".config" / "minebot" / "runtime.env"
    xdg_profile.parent.mkdir(parents=True)
    xdg_profile.write_text("VALUE=xdg\n", encoding="utf-8")
    assert discover_runtime_env_path(
        environ={},
        home=home,
        repository_root=repository,
    ) == xdg_profile.resolve()


def test_runtime_env_discovery_accepts_one_named_repo_profile(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    profile = repository / "config" / "local.env"
    profile.parent.mkdir(parents=True)
    profile.write_text("VALUE=local\n", encoding="utf-8")

    assert discover_runtime_env_path(
        environ={},
        home=tmp_path / "home",
        repository_root=repository,
    ) == profile.resolve()


def test_runtime_env_discovery_rejects_ambiguous_repo_profiles(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    config_dir = repository / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "first.env").write_text("VALUE=first\n", encoding="utf-8")
    (config_dir / "second.env").write_text("VALUE=second\n", encoding="utf-8")

    with pytest.raises(LocalLauncherError, match="multiple runtime env profiles"):
        discover_runtime_env_path(
            environ={},
            home=tmp_path / "home",
            repository_root=repository,
        )


def test_runtime_env_init_creates_private_local_template(tmp_path: Path) -> None:
    profile = tmp_path / "minebot" / "runtime.env"

    initialized, created = initialize_runtime_env(profile, environ={})

    assert created is True
    assert initialized == profile.resolve()
    assert profile.stat().st_mode & 0o077 == 0
    document = profile.read_text(encoding="utf-8")
    assert "MINEBOT_REAL_RCON_HOST=127.0.0.1" in document
    assert "MINEBOT_LLM_API_KEY=\n" in document
    assert "sk-" not in document

    initialized_again, created_again = initialize_runtime_env(profile, environ={})
    assert initialized_again == initialized
    assert created_again is False


def test_reset_is_restricted_to_loopback() -> None:
    _require_local_reset("localhost")
    with pytest.raises(LocalLauncherError, match="loopback"):
        _require_local_reset("example.org")


def test_main_composes_reset_camera_and_production_session_without_secret_argv(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "runtime.env"
    profile.write_text("MINEBOT_LLM_API_KEY=profile-secret\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    server = SimpleNamespace(rcon=SimpleNamespace(host="127.0.0.1"))
    camera_path = tmp_path / "camera.toml"
    completed = [
        subprocess.CompletedProcess(["reset"], 0),
        subprocess.CompletedProcess(["session"], 7),
    ]

    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "minebot.app.local_launcher.preflight_runtime_environment",
            return_value=(server, camera_path),
        ),
        patch("minebot.app.local_launcher.subprocess.run", side_effect=completed) as run,
    ):
        result = main(
            [
                "collect wood and make tools",
                "--env-file",
                str(profile),
                "--run-dir",
                str(run_dir),
            ]
        )

    assert result == 7
    assert run.call_count == 2
    reset_call = run.call_args_list[0]
    session_call = run.call_args_list[1]
    assert "profile-secret" not in repr(reset_call.args[0])
    assert "MINEBOT_LLM_API_KEY" not in reset_call.kwargs["env"]
    assert session_call.kwargs["env"]["MINEBOT_LLM_API_KEY"] == "profile-secret"
    assert session_call.kwargs["env"]["MINEBOT_REAL_RCON_HOST"] == "127.0.0.1"
    assert session_call.kwargs["env"]["MINEBOT_REAL_RCON_PORT"] == "25576"
    assert session_call.kwargs["env"]["MINEBOT_REAL_RCON_PASSWORD"] == "test"
    assert session_call.kwargs["env"]["MINEBOT_AGENT_LOG_PATH"] == str(
        run_dir / "trace.jsonl"
    )
    command = session_call.args[0]
    assert command[:3] == [sys.executable, "-m", "minebot.app.real_server_session"]
    assert "--interactive" in command
    assert "--camera" in command
    assert "profile-secret" not in repr(command)
