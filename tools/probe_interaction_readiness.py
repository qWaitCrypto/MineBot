#!/usr/bin/env python3
"""Loopback-only, no-model approach/collect proof in a verified empty fixture."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.probe_body_geometry import EMPTY_TYPES, await_body, forceload_add
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.body.world_read import read_block_facts
from minebot.contract import Region
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import GovernanceAnswerer, JavaBodyClient, websocket_transport
from minebot.game.rcon import RconClient, RconConfig


def main() -> int:
    bot = "ReadinessProbe"
    cells = tuple((x, y, z) for x in range(64, 72) for y in range(219, 224) for z in range(64, 71))
    target = (68, 220, 67)
    region = Region("readiness-probe", (63, 218, 63), (73, 225, 72))
    policy = GovernancePolicy(natural_regions=[region])
    written, loaded = [], []
    client = None
    body = None
    report = {"model_calls": 0, "pass": False}
    path = ROOT / "logs/agentic-runtime/interaction-readiness-20260908.json"
    with RconClient(RconConfig(host="127.0.0.1", port=25576,
                             password=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"))) as rcon:
        try:
            loaded = forceload_add(rcon, [(4, 4)])
            rcon.command(f"player {bot} spawn")
            rcon.command(f"gamemode spectator {bot}")
            client = JavaBodyClient(bot, websocket_transport("ws://127.0.0.1:8767"),
                                    GovernanceAnswerer(policy), action_wall_timeout_s=45, recv_timeout_s=2)
            client.connect()
            body = JavaBody(client, bot)
            await_body(body, rcon, bot)
            facts = read_block_facts(body, cells, page_size=256, failure_label="readiness empty volume")
            if len(facts) != len(cells) or any(str(f.data.get("type")) not in EMPTY_TYPES for f in facts.values()):
                raise RuntimeError("fixture volume is not empty")
            placements = {(x, 219, z): "stone" for x in range(64, 72) for z in range(64, 71)}
            placements.update({(67, y, z): "stone" for y in range(220, 223) for z in range(66, 69)})
            placements[target] = "oak_log"
            for pos, block in placements.items():
                written.append(pos)
                rcon.command(f"setblock {pos[0]} {pos[1]} {pos[2]} {block}")
            rcon.command(f"tp {bot} 65.5 220 67.5")
            rcon.command(f"gamemode survival {bot}")
            time.sleep(0.5)
            query = {"x": target[0], "y": target[1], "z": target[2], "interaction_radius": 4.5}
            before = body.perceive("blockAt", query)
            report["initial_readiness"] = before.data.get("interaction_ready")
            if not before.ok or report["initial_readiness"] is not False:
                raise RuntimeError("occluded target was not rejected")
            registry = build_phase1_registry(body, Phase1RuntimeConfig(
                natural_region=region, body_provider="java", governance_policy=policy))
            started = time.monotonic()
            approach = registry.get("get_to_block").callable({"block_types": ["oak_log"], "search_radius": 8})
            report["approach_s"] = time.monotonic() - started
            report["approach"] = approach.to_payload()
            if not approach.success:
                raise RuntimeError("approach failed")
            ready = body.perceive("blockAt", query)
            report["final_readiness"] = ready.data.get("interaction_ready")
            started = time.monotonic()
            collected = registry.get("mine_block_collect").callable({
                "pos": list(target), "target_block_types": ["oak_log"], "expected_drops": ["oak_log"],
            })
            report["collect_s"] = time.monotonic() - started
            report["collect"] = collected.to_payload()
            state = body.get_state()
            report["terminal_owner"] = state.body_owner
            report["terminal_pending"] = state.pending_action_count
            report["pass"] = (ready.ok and report["final_readiness"] is True and collected.success
                              and state.body_owner is None and state.pending_action_count == 0)
        except Exception as exc:
            report["error"] = type(exc).__name__
        finally:
            try:
                rcon.command(f"gamemode spectator {bot}")
                if body is not None:
                    body.interrupt()
                for x, y, z in written:
                    rcon.command(f"setblock {x} {y} {z} air")
                if written:
                    facts = read_block_facts(body, tuple(written), page_size=256, failure_label="readiness cleanup")
                    report["cleanup_residue"] = sum(str(f.data.get("type")) not in EMPTY_TYPES for f in facts.values())
                    report["cleanup_residue"] += len(written) - len(facts)
            except Exception as exc:
                report["cleanup_error"] = type(exc).__name__
                report["pass"] = False
            finally:
                try:
                    if client is not None:
                        client.close()
                    rcon.command(f"player {bot} kill")
                    for cx, cz in loaded:
                        rcon.command(f"forceload remove {cx * 16} {cz * 16}")
                    report["released_chunks"] = len(loaded)
                finally:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"approach", "collect"}}))
    return 0 if report["pass"] and report.get("cleanup_residue") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
