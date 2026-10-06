#!/usr/bin/env python3
"""Bounded, no-model production collection near the Gate 8 stone region."""

from __future__ import annotations

import json
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.probe_body_geometry import await_body
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Region
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import GovernanceAnswerer, JavaBodyClient, websocket_transport
from minebot.game.rcon import RconClient, RconConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "logs/agentic-runtime/natural-stone-latency-20260908.json")
    args = parser.parse_args()
    bot = "StoneSpeedProbe"
    region = Region("natural-stone-probe", (-120, 60, 30), (-70, 95, 85))
    policy = GovernancePolicy(natural_regions=[region])
    report = {"model_calls": 0, "world_reset": False, "max_wall_s": 120, "pass": False}
    path = args.out
    if path.exists():
        raise SystemExit("retain prior evidence before repeating this probe")
    client = None
    body = None
    with RconClient(RconConfig(host="127.0.0.1", port=25576,
                             password=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"))) as rcon:
        try:
            rcon.command(f"player {bot} spawn")
            client = JavaBodyClient(bot, websocket_transport("ws://127.0.0.1:8767"),
                                    GovernanceAnswerer(policy), action_wall_timeout_s=130, recv_timeout_s=2)
            client.connect()
            body = JavaBody(client, bot)
            await_body(body, rcon, bot)
            rcon.command(f"tp {bot} -96.5 82 54.5")
            rcon.command(f"gamemode survival {bot}")
            rcon.command(f"item replace entity {bot} weapon.mainhand with minecraft:wooden_pickaxe")
            time.sleep(1)
            before = body.get_state()
            report["initial_pos"] = list(before.pos)
            report["inventory_before"] = before.inventory_counts
            registry = build_phase1_registry(body, Phase1RuntimeConfig(
                natural_region=region, body_provider="java", governance_policy=policy))
            started = time.monotonic()
            result = registry.get("collect_block_domain").callable({
                "block_types": ["stone", "cobblestone"], "expected_drops": ["cobblestone"],
                "remaining_count": 3, "search_radius": 16, "candidate_budget": 96,
                "mutation_budget": 96, "max_wall_s": 120, "find_limit": 8, "max_pages": 8,
            })
            report["elapsed_s"] = time.monotonic() - started
            report["result"] = result.to_payload()
            after = body.get_state()
            report["final_pos"] = list(after.pos)
            report["inventory_after"] = after.inventory_counts
            report["terminal_owner"] = after.body_owner
            report["terminal_pending"] = after.pending_action_count
            report["pass"] = result.success and after.body_owner is None and after.pending_action_count == 0
        except Exception as exc:
            report["error"] = type(exc).__name__
        finally:
            try:
                if body is not None:
                    body.interrupt("natural_stone_probe_done")
                if client is not None:
                    client.close()
                rcon.command(f"player {bot} kill")
            finally:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "result"}))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
