from __future__ import annotations

import subprocess
import sys

from minebot.camera.launcher import java_proxy_arguments


def test_java_proxy_arguments_convert_standard_https_proxy_without_credentials() -> None:
    arguments = java_proxy_arguments({"HTTPS_PROXY": "http://operator:private@127.0.0.1:7897"})

    assert arguments == (
        "-Dhttp.proxyHost=127.0.0.1",
        "-Dhttp.proxyPort=7897",
        "-Dhttps.proxyHost=127.0.0.1",
        "-Dhttps.proxyPort=7897",
    )
    assert all("operator" not in argument and "private" not in argument for argument in arguments)


def test_java_proxy_arguments_support_socks_and_ignore_invalid_values() -> None:
    assert java_proxy_arguments({"ALL_PROXY": "socks5://[::1]:1080"}) == (
        "-DsocksProxyHost=::1",
        "-DsocksProxyPort=1080",
    )
    assert java_proxy_arguments({"HTTPS_PROXY": "ftp://proxy.example:21"}) == ()
    assert java_proxy_arguments({"HTTPS_PROXY": "http://proxy.example:not-a-port"}) == ()


def test_launcher_module_prints_only_jvm_arguments(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    result = subprocess.run(
        [sys.executable, "-m", "minebot.camera.launcher"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == list(java_proxy_arguments())
