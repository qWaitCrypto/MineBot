#!/usr/bin/env python3
"""No-model, loopback-only probe of bounded governed navigation continuation."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.probe_body_geometry import EMPTY_TYPES, await_body, forceload_add
from minebot.body.navigation import NavigationRunConfig, governed_mobility_navigation_config
from minebot.body.objective_navigation import ObjectiveNavigationTransactions
from minebot.body.world_read import read_block_facts
from minebot.contract import BreakContext, Region
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import GovernanceAnswerer, JavaBodyClient, websocket_transport
from minebot.game.rcon import RconClient, RconConfig


def main() -> int:
    bot = "NavSliceProbe"
    cells = tuple((x, y, z) for x in range(31, 43)
                  for y in range(219, 223) for z in range(31, 34))
    written = []
    denied = set()
    loaded = []
    client = None
    report = {"model_calls": 0, "world_reset": False, "server_restart": False}
    path = ROOT / "logs/agentic-runtime/governed-navigation-continuation-20260908-v3.json"
    with RconClient(RconConfig(host="127.0.0.1", port=25576,
                             password=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"))) as rcon:
        try:
            loaded = forceload_add(rcon, sorted({(x // 16, z // 16) for x, _, z in cells}))
            rcon.command(f"player {bot} spawn")
            rcon.command(f"gamemode spectator {bot}")
            policy = GovernancePolicy(natural_regions=[Region("slice-probe", (30, 218, 30), (44, 224, 34))])
            class RecordingAnswerer(GovernanceAnswerer):
                def verdict(self, proposal):
                    allowed, reason = super().verdict(proposal)
                    report.setdefault("governance", []).append({
                        "block": proposal.block_id, "allowed": allowed, "reason": reason,
                        "pos": [proposal.x, proposal.y, proposal.z],
                    })
                    return allowed, reason

            client = JavaBodyClient(bot, websocket_transport("ws://127.0.0.1:8767"),
                                    RecordingAnswerer(policy), action_wall_timeout_s=30, recv_timeout_s=2)
            client.connect()
            body = JavaBody(client, bot)
            await_body(body, rcon, bot)
            facts = read_block_facts(body, cells, page_size=256, failure_label="slice probe empty volume")
            if len(facts) != len(cells) or any(str(f.data.get("type")) not in EMPTY_TYPES for f in facts.values()):
                raise RuntimeError("fixture volume is not verified empty")
            for x, y, z in cells:
                block = None
                if y in (219, 222) or z in (31, 33) or x in (31, 42):
                    block = "bedrock"
                    if (y == 220 or (z == 32 and y in (219, 222))):
                        denied.add((x, y, z))
                elif 34 <= x <= 39:
                    block = "stone"
                if block:
                    written.append((x, y, z))
                    rcon.command(f"setblock {x} {y} {z} {block}")
            rcon.command(f"tp {bot} 32.5 220 32.5")
            rcon.command(f"gamemode survival {bot}")
            rcon.command(f"item replace entity {bot} weapon.mainhand with minecraft:stone_pickaxe")
            time.sleep(1)
            started = time.monotonic()
            result = ObjectiveNavigationTransactions(body).navigate_to(
                (40, 220, 32), break_context=BreakContext.COLLECT_APPROACH,
                config=governed_mobility_navigation_config(NavigationRunConfig(
                    max_break_steps=16, max_segments=10, segment_timeout_s=3,
                )), continuation_check=lambda: None, mutation_blacklist=denied,
            )
            report.update(result=result.to_payload(), elapsed_s=time.monotonic() - started,
                          final_pos=list(body.get_state().pos))
            slices = (result.metrics or {}).get("navigation_slices", [])
            report["continued"] = len(slices) > 1
            remaining = 16
            receipts_valid = True
            for entry in slices:
                metrics = entry.get("metrics") or {}
                used = metrics.get("mutations_completed")
                left = metrics.get("budget_remaining")
                if not (type(used) is int and type(left) is int
                        and 0 <= used <= remaining and left == remaining - used):
                    receipts_valid = False
                    break
                remaining = left
            report["receipts_valid"] = receipts_valid
            report["pass"] = result.success and len(slices) > 1 and receipts_valid
            print(json.dumps({k: v for k, v in report.items() if k != "result"}), flush=True)
        finally:
            rcon.command(f"gamemode spectator {bot}")
            for x, y, z in written:
                rcon.command(f"setblock {x} {y} {z} air")
            report["cleanup_cells"] = len(written)
            try:
                if written:
                    restored = read_block_facts(body, tuple(written), page_size=256,
                                               failure_label="slice probe cleanup")
                    report["cleanup_residue"] = sum(
                        str(f.data.get("type")) not in EMPTY_TYPES for f in restored.values()
                    ) + len(written) - len(restored)
                else:
                    report["cleanup_residue"] = 0
            finally:
                if client is not None:
                    client.close()
                rcon.command(f"player {bot} kill")
                for cx, cz in loaded:
                    rcon.command(f"forceload remove {cx * 16} {cz * 16}")
            report["released_chunks"] = len(loaded)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("pass") and not report.get("cleanup_residue") else 1


if __name__ == "__main__":
    raise SystemExit(main())
