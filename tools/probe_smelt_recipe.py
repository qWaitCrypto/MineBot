#!/usr/bin/env python3
"""Probe whether Scarpet recipe_data supports smelting recipe lookup."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game.errors import RconError
from minebot.game.rcon import RconClient, RconConfig
from tests.e2e_support import SKIP_EXIT_CODE


PROBE_EXPRESSIONS = (
    "recipe_data('minecraft:iron_ingot', 'smelting')",
    "recipe_data('minecraft:iron_ingot', 'minecraft:smelting')",
    "recipe_data('minecraft:iron_ingot', l('smelting'))",
)


def main() -> None:
    config = RconConfig()
    try:
        with RconClient(config) as rcon:
            results = [_probe_expression(rcon, expr) for expr in PROBE_EXPRESSIONS]
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    supported = any(row["supported"] for row in results)
    payload = {"supported": supported, "results": results}
    print(json.dumps(payload, sort_keys=True))
    if not supported:
        raise SystemExit(2)


def _probe_expression(rcon: RconClient, expression: str) -> dict[str, object]:
    output = rcon.command(f"script in minebot run {expression}")
    lowered = output.lower()
    supported = "error while evaluating expression" not in lowered and "unknown function" not in lowered
    supported = supported and "null" not in lowered and "[]" not in lowered
    return {"expression": expression, "supported": supported, "output": output[:1000]}


if __name__ == "__main__":
    main()
