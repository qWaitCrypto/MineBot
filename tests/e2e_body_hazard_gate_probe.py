#!/usr/bin/env python3
"""Bounded live probe for the unresolved survival-hazard action gate."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import ScarpetBody  # noqa: E402
from tests.e2e_body_reflex_preemption import (  # noqa: E402
    BOT,
    BASE,
    run_water_unresolved_action_gate,
    setup_world,
)
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


def main() -> int:
    with connect_or_skip() as rcon:
        setup_world(rcon)
        body = ScarpetBody(BOT, rcon)
        try:
            spawn_or_fail(body, BASE)
            run_water_unresolved_action_gate(rcon, body)
            print("PASS unresolved_hazard_gate: ordinary actions blocked; marked recovery cleared hazard")
        finally:
            rcon.command("script in minebot run global_reflex_scan = false")
            rcon.command(f"player {BOT} kill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
