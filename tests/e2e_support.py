"""Shared helpers for body-layer end-to-end tests.

This is the **outer shell only** — the agreed scope with codex. It owns the
connection boilerplate and the SKIP-77 / ``MINEBOT_E2E_REQUIRED`` policy that
every e2e repeats. It deliberately does **not** own per-test world-setup
semantics, coordinate regions, or bot naming: each e2e keeps its own
``setup_world()`` / ``reset_*()`` and runs serially/exclusively (codex's stated
execution model). Do not use this module to batch-share mutable world state.
"""

from __future__ import annotations

import contextlib
import os
from typing import Iterator

from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import RconError
from minebot.game.rcon import RconConfig

#: Exit-code convention shared by the whole e2e batch (codex's rule):
#:   0 = pass, 77 = SKIP (live server unavailable), any other non-zero = fail.
SKIP_EXIT_CODE = 77


def spawn_or_fail(body: ScarpetBody, pos: tuple[int, int, int], *, timeout_s: float = 30.0) -> None:
    result = body.spawn(pos, timeout_s=timeout_s)
    if not (result.ok and result.accepted):
        raise AssertionError(f"spawn failed: {result}")


@contextlib.contextmanager
def connect_or_skip(config: RconConfig | None = None) -> Iterator[RconClient]:
    """Open an RCON connection, or skip the test (exit 77) when unreachable.

    Collapses the boilerplate every e2e repeats into one line::

        with connect_or_skip() as rcon:
            setup_world(rcon)
            ...

    Policy (verbatim from the existing e2e files, codex's convention):
    - On connect failure (``OSError`` / ``PermissionError`` / ``RconError``):
      if ``MINEBOT_E2E_REQUIRED=1`` is set, re-raise so a CI run treats an
      unreachable server as a hard failure; otherwise print a ``SKIP:`` line and
      ``raise SystemExit(77)`` so the batch runner classifies the test as
      *skipped*, not *failed*.

    The connection is always closed on exit (even on exception). This is the
    **shell** — it performs no world setup; the caller still owns its
    ``setup_world()``. ``config`` defaults to ``RconConfig()`` (the local
    test-server) but may be passed for tests that target a second client or a
    non-default endpoint.
    """
    config = config or RconConfig()
    rcon = RconClient(config)
    try:
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(
            f"SKIP: local RCON unavailable at {config.host}:{config.port}: "
            f"{type(exc).__name__}: {exc}"
        )
        raise SystemExit(SKIP_EXIT_CODE)
    try:
        yield rcon
    finally:
        rcon.close()


__all__ = ["SKIP_EXIT_CODE", "connect_or_skip", "spawn_or_fail"]
