#!/usr/bin/env python3
"""Loopback proof that resource collection skips an unready target and collects another."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
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


class RecordingTransport:
    """Observe production wire requests without changing their behavior."""

    def __init__(self, inner, sent: list[dict[str, object]]) -> None:
        self._inner = inner
        self._sent = sent

    def send(self, text: str) -> None:
        message = json.loads(text)
        if isinstance(message, dict):
            self._sent.append(message)
        self._inner.send(text)

    def recv(self, timeout: float) -> str:
        return self._inner.recv(timeout)

    def close(self) -> None:
        self._inner.close()


def main() -> int:
    bot = "ResFallback"
    target_a = (68, 220, 67)
    target_b = (73, 220, 67)
    cells = tuple(
        (x, y, z)
        for x in range(64, 76)
        for y in range(219, 224)
        for z in range(63, 72)
    )
    region = Region("resource-fallback-probe", (63, 218, 62), (77, 225, 72))
    policy = GovernancePolicy(natural_regions=[region])
    sent: list[dict[str, object]] = []
    written: list[tuple[int, int, int]] = []
    loaded: list[tuple[int, int]] = []
    client = None
    body = None
    report: dict[str, object] = {
        "model_calls": 0,
        "pass": False,
        "targets": {"rejected": list(target_a), "fallback": list(target_b)},
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / "logs/agentic-runtime" / f"resource-readiness-fallback-{stamp}.json"
    base_transport = websocket_transport("ws://127.0.0.1:8767")

    def transport():
        return RecordingTransport(base_transport(), sent)

    with RconClient(
        RconConfig(
            host="127.0.0.1",
            port=25576,
            password=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"),
        )
    ) as rcon:
        try:
            loaded = forceload_add(rcon, [(4, 4)])
            rcon.command(f"player {bot} spawn")
            rcon.command(f"gamemode spectator {bot}")
            client = JavaBodyClient(
                bot,
                transport,
                GovernanceAnswerer(policy),
                action_wall_timeout_s=60,
                recv_timeout_s=2,
            )
            client.connect()
            body = JavaBody(client, bot)
            await_body(body, rcon, bot)
            facts = read_block_facts(body, cells, page_size=256, failure_label="fallback empty volume")
            if len(facts) != len(cells) or any(
                str(fact.data.get("type")) not in EMPTY_TYPES for fact in facts.values()
            ):
                raise RuntimeError("fixture volume is not empty")

            placements = {
                (x, 219, z): "stone"
                for x in range(64, 76)
                for z in range(63, 72)
            }
            # A is visible to discovery but starts behind a three-wide wall.
            placements.update(
                ( (67, y, z), "stone")
                for y in range(220, 223)
                for z in range(66, 69)
            )
            placements[target_a] = "oak_log"
            placements[target_b] = "birch_log"
            for pos, block in placements.items():
                written.append(pos)
                rcon.command(f"setblock {pos[0]} {pos[1]} {pos[2]} {block}")

            rcon.command(f"tp {bot} 65.5 220 67.5")
            rcon.command(f"gamemode survival {bot}")
            time.sleep(0.5)
            before_counts = body.get_state().inventory_counts
            registry = build_phase1_registry(
                body,
                Phase1RuntimeConfig(
                    natural_region=region,
                    body_provider="java",
                    governance_policy=policy,
                ),
            )
            started = time.monotonic()
            result = registry.get("collect_block_domain").callable(
                {
                    "block_types": ["oak_log", "birch_log"],
                    "expected_drops": ["oak_log", "birch_log"],
                    "remaining_count": 1,
                    "search_radius": 10,
                    "candidate_budget": 2,
                    "mutation_budget": 2,
                    "max_wall_s": 50,
                    "find_limit": 8,
                    "max_pages": 1,
                    "segment_timeout_s": 10,
                }
            )
            report["elapsed_s"] = round(time.monotonic() - started, 3)
            report["result"] = result.to_payload()
            after = body.get_state()
            attempts = list((result.metrics or {}).get("attempts") or [])
            rejected = [
                attempt
                for attempt in attempts
                if (attempt.get("interaction_readiness") or {}).get("reason")
                == "resource_target_not_interactable"
            ]
            collect_targets = [
                list(message.get("target") or [])
                for message in sent
                if message.get("type") == "COLLECT_BLOCK"
            ]
            delta = {
                item: int(after.inventory_counts.get(item, 0)) - int(before_counts.get(item, 0))
                for item in ("oak_log", "birch_log")
            }
            report.update(
                {
                    "rejected_attempts": rejected,
                    "collect_targets": collect_targets,
                    "inventory_delta": delta,
                    "terminal_owner": after.body_owner,
                    "terminal_pending": after.pending_action_count,
                }
            )
            report["pass"] = bool(
                result.success
                and rejected
                and list(target_a) not in collect_targets
                and list(target_b) in collect_targets
                and delta["birch_log"] >= 1
                and after.body_owner is None
                and after.pending_action_count == 0
            )
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                rcon.command(f"gamemode spectator {bot}")
                if body is not None:
                    body.interrupt()
                for x, y, z in reversed(written):
                    rcon.command(f"setblock {x} {y} {z} air")
                if body is not None and written:
                    facts = read_block_facts(
                        body,
                        tuple(written),
                        page_size=256,
                        failure_label="fallback cleanup",
                    )
                    report["cleanup_residue"] = sum(
                        str(fact.data.get("type")) not in EMPTY_TYPES
                        for fact in facts.values()
                    ) + len(written) - len(facts)
            except Exception as exc:
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                report["pass"] = False
            finally:
                if client is not None:
                    client.close()
                rcon.command(f"player {bot} kill")
                for cx, cz in loaded:
                    rcon.command(f"forceload remove {cx * 16} {cz * 16}")
                report["released_chunks"] = len(loaded)
                if report.get("cleanup_residue") != 0:
                    report["pass"] = False
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x", encoding="utf-8") as handle:
                    json.dump(report, handle, indent=2)
                    handle.write("\n")

    print(json.dumps({k: v for k, v in report.items() if k not in {"result", "rejected_attempts"}}))
    print(path)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
