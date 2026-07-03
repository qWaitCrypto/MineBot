#!/usr/bin/env python3
"""Probe W1 through MineBot's existing search stack without an LLM."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Region
from minebot.game import RconClient, ScarpetBody
from minebot.game.rcon import RconConfig

LOG_TYPES = ("oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run search_for_block against W1 through the Body stack.")
    parser.add_argument("--bot", default=os.environ.get("MINEBOT_REAL_BOT", "MineBotReal"))
    parser.add_argument("--radius", type=int, default=32)
    parser.add_argument("--find-limit", type=int, default=12)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = _rcon_config_from_env()
    with RconClient(config) as rcon:
        body = ScarpetBody(args.bot, rcon)
        registry = build_phase1_registry(
            body,
            Phase1RuntimeConfig(natural_region=Region("real", (-512, -64, -512), (512, 320, 512))),
        )
        result = registry.get("search_for_block").callable(
            {
                "block_types": list(LOG_TYPES),
                "search_radius": args.radius,
                "find_limit": args.find_limit,
                "max_pages": args.max_pages,
            }
        )
    payload = result.to_payload() if hasattr(result, "to_payload") else result
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
        candidates = metrics.get("candidates") if isinstance(metrics, dict) else None
        print(
            f"success={payload.get('success')} reason={payload.get('reason')} "
            f"candidate_count={len(candidates) if isinstance(candidates, list) else 0}"
        )
        target = metrics.get("target") if isinstance(metrics, dict) else None
        if target:
            print(f"target={target}")
    return 0 if payload.get("success") else 2


def _rcon_config_from_env() -> RconConfig:
    return RconConfig(
        host=os.environ.get("MINEBOT_REAL_RCON_HOST", "127.0.0.1"),
        port=int(os.environ.get("MINEBOT_REAL_RCON_PORT", "25576")),
        password=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"),
        timeout_s=float(os.environ.get("MINEBOT_REAL_RCON_TIMEOUT", "30")),
        reconnect_attempts=2,
    )


if __name__ == "__main__":
    raise SystemExit(main())
