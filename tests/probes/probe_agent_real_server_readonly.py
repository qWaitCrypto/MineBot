#!/usr/bin/env python3
"""Read-only real-server probe for the Agent Harness Q0 audit.

This probe is intentionally conservative. It does not spawn, teleport, clear,
load scripts, change gamerules, place blocks, or execute body actions. It only
connects to an explicitly configured RCON endpoint and reads server/bot facts.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var {name}")
    return value


def main() -> int:
    host = env_required("MINEBOT_REAL_RCON_HOST")
    port = int(env_required("MINEBOT_REAL_RCON_PORT"))
    password = env_required("MINEBOT_REAL_RCON_PASSWORD")
    bot_name = env_required("MINEBOT_REAL_BOT")
    timeout_s = float(os.environ.get("MINEBOT_REAL_RCON_TIMEOUT", "20"))

    config = RconConfig(host=host, port=port, password=password, timeout_s=timeout_s)
    rcon = RconClient(config)
    try:
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "phase": "connect",
                    "host": host,
                    "port": port,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2

    with rcon:
        body = ScarpetBody(bot_name, rcon)
        version = rcon.command("version")
        list_players = rcon.command("list")
        try:
            state = body.get_state()
            state_payload = asdict(state)
        except Exception as exc:  # noqa: BLE001 - probe must report exact failure
            state_payload = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        try:
            nearby = body.perceive("nearbyBlocks", {"radius": 4, "limit": 8})
            nearby_payload = {
                "ok": nearby.ok,
                "complete": nearby.complete,
                "error": nearby.error,
                "uncertainty": nearby.uncertainty,
                "data_keys": sorted((nearby.data or {}).keys()),
            }
        except Exception as exc:  # noqa: BLE001
            nearby_payload = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    print(
        json.dumps(
            {
                "ok": True,
                "phase": "readonly_probe",
                "host": host,
                "port": port,
                "bot": bot_name,
                "version": version,
                "list": list_players,
                "state": state_payload,
                "nearby_blocks": nearby_payload,
                "mutation": "none",
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
