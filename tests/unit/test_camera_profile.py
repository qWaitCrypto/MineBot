from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from minebot.camera.profile import bootstrap_observer_profile


def test_bootstrap_observer_profile_creates_the_required_noninteractive_options(tmp_path: Path) -> None:
    profile = tmp_path / "observer"

    options_path = bootstrap_observer_profile(profile)

    assert options_path == profile / "options.txt"
    assert options_path.read_text(encoding="utf-8").splitlines() == [
        "pauseOnLostFocus:false",
        "skipMultiplayerWarning:true",
        "joinedFirstServer:true",
        "onboardAccessibility:false",
    ]


def test_bootstrap_observer_profile_preserves_other_options_and_deduplicates_controlled_keys(tmp_path: Path) -> None:
    profile = tmp_path / "observer"
    profile.mkdir()
    options_path = profile / "options.txt"
    options_path.write_text(
        "lang:en_us\nonboardAccessibility:true\nkey_key.forward:key.keyboard.w\nonboardAccessibility:true\n",
        encoding="utf-8",
    )

    bootstrap_observer_profile(profile)

    lines = options_path.read_text(encoding="utf-8").splitlines()
    assert "lang:en_us" in lines
    assert "key_key.forward:key.keyboard.w" in lines
    assert lines.count("onboardAccessibility:false") == 1
    assert "pauseOnLostFocus:false" in lines
    assert "skipMultiplayerWarning:true" in lines
    assert "joinedFirstServer:true" in lines


def test_profile_bootstrap_module_is_invocable_by_the_observer_launcher(tmp_path: Path) -> None:
    profile = tmp_path / "observer"

    result = subprocess.run(
        [sys.executable, "-m", "minebot.camera.profile", str(profile)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert Path(result.stdout.strip()) == profile / "options.txt"
