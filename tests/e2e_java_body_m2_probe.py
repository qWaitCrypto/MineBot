"""Bounded live probe for governed COLLECT_BLOCK. Not a gate.

Two cases against the local test server, natural world targets only:

- deny path: every MUTATION_PROPOSAL is denied by probe policy; the terminal
  must be a typed governance failure and the proposed block must still exist.
- allow path: proposals for natural log-family blocks are allowed (probe
  policy standing in for the production governance integration, recorded as
  such); success requires a mutation_verified event and an authoritative
  inventory delta, cross-checked through RCON entity data.

Bot placement near trees is bounded scenario setup; target discovery,
approach, stand selection, and mining are fully natural. formal_gate: false.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from websockets.sync.client import connect

from minebot.game.java_body_protocol import (
    BotEvent,
    ErrorResponse,
    JavaBodyProtocol,
    Response,
    ServerProposal,
)
from minebot.game.rcon import RconClient, RconConfig

BODY_URL = "ws://127.0.0.1:8767"
BOT_NAME = "JavaBodyProbe"
LOG_FAMILY = [
    "minecraft:oak_log",
    "minecraft:spruce_log",
    "minecraft:birch_log",
    "minecraft:dark_oak_log",
]
CASE_WALL_TIMEOUT_S = 180.0


class Probe:
    def __init__(self) -> None:
        self.socket = connect(BODY_URL, open_timeout=10)
        self.protocol = JavaBodyProtocol()
        self.events: list[BotEvent] = []
        self.proposals: list[dict] = []
        self.verdict_policy = None  # callable(ServerProposal) -> (allow, reason)

    def _handle(self, item) -> None:
        if isinstance(item, BotEvent):
            self.events.append(item)
        elif isinstance(item, ServerProposal):
            received = time.monotonic()
            allow, reason = self.verdict_policy(item)
            self.socket.send(json.dumps(self.protocol.mutation_verdict(item.proposal_id, allow, reason)))
            self.proposals.append(
                {
                    "proposal_id": item.proposal_id,
                    "block_id": item.block_id,
                    "pos": [item.x, item.y, item.z],
                    "allow": allow,
                    "reason": reason,
                    "client_turnaround_ms": round((time.monotonic() - received) * 1000.0, 3),
                }
            )

    def request(self, message: dict, timeout_s: float = 15.0):
        self.socket.send(json.dumps(message))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for item in self.protocol.feed(json.loads(self.socket.recv(timeout=deadline - time.monotonic()))):
                if isinstance(item, (Response, ErrorResponse)):
                    return item
                self._handle(item)
        raise TimeoutError(message["type"])

    def wait_terminal(self, action_id: str, timeout_s: float) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                for item in self.protocol.feed(json.loads(self.socket.recv(timeout=2.0))):
                    self._handle(item)
            except TimeoutError:
                continue
            for event in self.events:
                if event.name == "action_terminal" and event.action_id == action_id:
                    return event.data
        return None

    def action_events(self, action_id: str) -> list[dict]:
        return [
            {"seq": e.seq, "tick": e.tick, "event": e.name, "data": e.data}
            for e in self.events
            if e.action_id == action_id
        ]


def block_exists(probe: Probe, pos: list[int], block_id: str) -> bool:
    result = probe.request(probe.protocol.find_blocks(BOT_NAME, [block_id], 16, vertical_radius=16, limit=64))
    if isinstance(result, ErrorResponse):
        return False
    return any([m["x"], m["y"], m["z"]] == pos for m in result.payload.get("matches", []))


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    rcon.command(f"player {BOT_NAME} spawn")
    time.sleep(2)
    # Survival rules: creative breaking is instant and drops nothing, which
    # would void the pickup-truth half of the slice.
    rcon.command(f"gamemode survival {BOT_NAME}")
    # Bounded scenario setup: drop near the natural tree cluster, then use the
    # Body's own perception to pick ground at tree level — a grass block close
    # to a log candidate — and settle on it. Discovery stays fully natural.
    rcon.command(f"tp {BOT_NAME} 60 120 -50")
    time.sleep(4)

    probe = Probe()
    hello = probe.request(probe.protocol.hello())
    assert isinstance(hello, Response), hello

    logs_result = probe.request(probe.protocol.find_blocks(BOT_NAME, LOG_FAMILY, 32, vertical_radius=48, limit=64))
    grass_result = probe.request(
        probe.protocol.find_blocks(BOT_NAME, ["minecraft:grass_block"], 32, vertical_radius=48, limit=64)
    )
    logs = logs_result.payload.get("matches", []) if isinstance(logs_result, Response) else []
    grasses = grass_result.payload.get("matches", []) if isinstance(grass_result, Response) else []

    def near_tree_ground(grass: dict) -> float:
        best = float("inf")
        for log in logs:
            if abs(grass["y"] + 1 - log["y"]) <= 6:
                dx = grass["x"] - log["x"]
                dz = grass["z"] - log["z"]
                best = min(best, dx * dx + dz * dz)
        return best

    stand = min(grasses, key=near_tree_ground, default=None)
    if stand is not None and near_tree_ground(stand) < float("inf"):
        rcon.command(f"tp {BOT_NAME} {stand['x']} {stand['y'] + 2} {stand['z']}")
        time.sleep(3)
    start_pos = rcon.command(f"data get entity {BOT_NAME} Pos")

    artifact: dict = {
        "scope": "java_body_m2_governed_collect_probe",
        "formal_gate": False,
        "bounded": True,
        "policy_note": "probe verdicts stand in for production governance integration: deny-all in case A, natural-log-family allow in case B",
        "start_pos_raw": start_pos,
        "cases": {},
    }

    # ---- Case A: deny path -------------------------------------------------
    probe.verdict_policy = lambda proposal: (False, "probe_policy_deny_test")
    action_a = f"m2-deny-{int(time.time())}"
    ack = probe.request(probe.protocol.collect_block(BOT_NAME, action_a, LOG_FAMILY, radius=32, timeout_ticks=2400))
    case_a: dict = {"action_id": action_a}
    if isinstance(ack, ErrorResponse):
        case_a["ack_error"] = ack.payload
    else:
        case_a["ack"] = {"state": ack.payload.get("state"), "candidates": ack.payload.get("candidates")}
        terminal = probe.wait_terminal(action_a, CASE_WALL_TIMEOUT_S)
        case_a["terminal"] = terminal
        case_a["proposals"] = [p for p in probe.proposals]
        if probe.proposals:
            first = probe.proposals[0]
            case_a["denied_block_still_present"] = block_exists(probe, first["pos"], first["block_id"])
        case_a["mutation_verified_events"] = sum(
            1 for e in probe.events if e.name == "mutation_verified" and e.action_id == action_a
        )
    artifact["cases"]["deny"] = case_a

    # ---- Case B: allow path ------------------------------------------------
    proposals_before = len(probe.proposals)
    probe.verdict_policy = lambda proposal: (
        (True, "natural_log_family_allow_probe_policy")
        if proposal.block_id in LOG_FAMILY and proposal.kind == "break"
        else (False, "outside_probe_allow_class")
    )
    action_b = f"m2-allow-{int(time.time())}"
    ack = probe.request(probe.protocol.collect_block(BOT_NAME, action_b, LOG_FAMILY, radius=32, timeout_ticks=2400))
    case_b: dict = {"action_id": action_b}
    if isinstance(ack, ErrorResponse):
        case_b["ack_error"] = ack.payload
    else:
        case_b["ack"] = {"state": ack.payload.get("state"), "candidates": ack.payload.get("candidates")}
        terminal = probe.wait_terminal(action_b, CASE_WALL_TIMEOUT_S)
        case_b["terminal"] = terminal
        case_b["proposals"] = probe.proposals[proposals_before:]
        case_b["events"] = probe.action_events(action_b)
        inventory_raw = rcon.command(f"data get entity {BOT_NAME} Inventory")
        case_b["rcon_inventory_has_log"] = bool(re.search(r"_log", inventory_raw))
        case_b["rcon_inventory_excerpt"] = inventory_raw[:400]
    artifact["cases"]["allow"] = case_b

    out = Path("logs/agentic-runtime/java-body-m2-governed-collect-20260726.json")
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
