from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit


_PROXY_ENV_NAMES = (
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
)


def java_proxy_arguments(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Translate an existing standard proxy environment into credential-free JVM flags."""
    environment = os.environ if environ is None else environ
    for name in _PROXY_ENV_NAMES:
        value = (environment.get(name) or "").strip()
        arguments = _proxy_arguments(value)
        if arguments:
            return arguments
    return ()


def _proxy_arguments(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    parsed = urlsplit(value if "://" in value else f"http://{value}")
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if host is None or not host or any(character.isspace() for character in host):
        return ()
    try:
        port = parsed.port
    except ValueError:
        return ()
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65_535:
        return ()

    if scheme in {"http", "https"}:
        return (
            f"-Dhttp.proxyHost={host}",
            f"-Dhttp.proxyPort={port}",
            f"-Dhttps.proxyHost={host}",
            f"-Dhttps.proxyPort={port}",
        )
    if scheme in {"socks", "socks4", "socks5"}:
        return (f"-DsocksProxyHost={host}", f"-DsocksProxyPort={port}")
    return ()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments:
        raise SystemExit("minebot.camera.launcher takes no arguments")
    print("\n".join(java_proxy_arguments()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
