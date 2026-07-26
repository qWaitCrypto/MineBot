"""Bounded live probe for the Java Body M1 surface. Not a gate.

Runs against the local test server: HELLO negotiation, FIND_BLOCKS latency
sampling at radius 32 and 128 against the frozen budgets, then one NAVIGATE
with an interact goal at the nearest found log, watching pushed events to the
typed terminal and reconciling with QUERY_ACTION. Writes a JSON artifact under
logs/agentic-runtime/; formal_gate is always false.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

from websockets.sync.client import connect

from minebot.game.java_body_protocol import (
    BotEvent,
    ErrorResponse,
    EventGap,
    JavaBodyProtocol,
    Response,
)

BODY_URL = "ws://127.0.0.1:8767"
BOT_NAME = "JavaBodyProbe"
LOG_BLOCK_IDS = [
    "minecraft:oak_log",
    "minecraft:spruce_log",
    "minecraft:birch_log",
    "minecraft:jungle_log",
    "minecraft:acacia_log",
    "minecraft:dark_oak_log",
    "minecraft:mangrove_log",
    "minecraft:cherry_log",
]
SAMPLES_PER_RADIUS = 30
BUDGET_P95_MS = {32: 100.0, 128: 300.0}
NAVIGATE_WALL_TIMEOUT_S = 150.0


class ProbeConnection:
    def __init__(self, url: str) -> None:
        self.socket = connect(url, open_timeout=10)
        self.protocol = JavaBodyProtocol()
        self.events: list[BotEvent] = []
        self.gaps: list[EventGap] = []

    def request(self, message: dict, timeout_s: float = 10.0) -> Response | ErrorResponse:
        self.socket.send(json.dumps(message))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = json.loads(self.socket.recv(timeout=deadline - time.monotonic()))
            for item in self.protocol.feed(frame):
                if isinstance(item, (Response, ErrorResponse)):
                    return item
                if isinstance(item, BotEvent):
                    self.events.append(item)
                elif isinstance(item, EventGap):
                    self.gaps.append(item)
        raise TimeoutError(f"no response to {message['type']} within {timeout_s}s")

    def pump_events(self, timeout_s: float) -> None:
        try:
            frame = json.loads(self.socket.recv(timeout=timeout_s))
        except TimeoutError:
            return
        for item in self.protocol.feed(frame):
            if isinstance(item, BotEvent):
                self.events.append(item)
            elif isinstance(item, EventGap):
                self.gaps.append(item)


def main() -> int:
    artifact: dict = {
        "scope": "java_body_m1_live_probe",
        "formal_gate": False,
        "bounded": True,
        "commit": None,
        "server": BODY_URL,
        "bot": BOT_NAME,
    }
    started = time.monotonic()
    conn = ProbeConnection(BODY_URL)

    hello = conn.request(conn.protocol.hello())
    assert isinstance(hello, Response), hello
    artifact["hello"] = {
        "minecraft_version": hello.payload.get("minecraft_version"),
        "request_types": hello.payload.get("request_types"),
        "max_request_bytes": hello.payload.get("max_request_bytes"),
    }

    # --- FIND_BLOCKS latency sampling -------------------------------------
    perf: dict = {}
    nearest_match: dict | None = None
    for radius in (32, 128):
        latencies: list[float] = []
        coverage: dict = {}
        for _ in range(SAMPLES_PER_RADIUS):
            time.sleep(0.06)  # stay under the 40/s transport rate limit
            t0 = time.monotonic()
            result = conn.request(
                conn.protocol.find_blocks(BOT_NAME, LOG_BLOCK_IDS, radius, vertical_radius=16, limit=64)
            )
            latencies.append((time.monotonic() - t0) * 1000.0)
            if isinstance(result, ErrorResponse):
                artifact["error"] = {"stage": f"find_blocks_r{radius}", "code": result.code, "message": result.message}
                write_artifact(artifact)
                return 1
            coverage = {
                "coverage_complete": result.payload.get("coverage_complete"),
                "unloaded_chunk_count": result.payload.get("unloaded_chunk_count"),
                "result_capped": result.payload.get("result_capped"),
                "matches": len(result.payload.get("matches", [])),
                "index_generation": result.payload.get("index_generation"),
            }
            matches = result.payload.get("matches", [])
            if matches and (nearest_match is None or matches[0]["distance_squared"] < nearest_match["distance_squared"]):
                nearest_match = matches[0]
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95) - 1]
        perf[f"radius_{radius}"] = {
            "samples": len(latencies),
            "p50_ms": round(statistics.median(latencies), 3),
            "p95_ms": round(p95, 3),
            "max_ms": round(latencies[-1], 3),
            "budget_p95_ms": BUDGET_P95_MS[radius],
            "within_budget": p95 <= BUDGET_P95_MS[radius],
            "final_coverage": coverage,
        }
    artifact["find_blocks_perf"] = perf

    # --- NAVIGATE vertical step -------------------------------------------
    # Natural candidates only: iterate the nearest matches until one is
    # walk-reachable; every typed failure is recorded, never retried blindly.
    candidates = candidate_targets(conn)
    if not candidates:
        artifact["navigate"] = {"skipped": "no_log_candidates_found"}
        write_artifact(artifact)
        return 0

    attempts: list[dict] = []
    completed = None
    for index, target in enumerate(candidates[:4]):
        action_id = f"probe-nav-{int(time.time())}-{index}"
        goal = {"kind": "interact", "x": target["x"], "y": target["y"], "z": target["z"], "range": 4.5}
        attempt: dict = {"target": target, "action_id": action_id}
        ack = conn.request(conn.protocol.navigate(BOT_NAME, action_id, goal, timeout_ticks=2400))
        if isinstance(ack, ErrorResponse):
            attempt["ack_error"] = {"code": ack.code, "payload": ack.payload}
            attempts.append(attempt)
            break
        attempt["ack_state"] = ack.payload.get("state")

        terminal: dict | None = None
        deadline = time.monotonic() + NAVIGATE_WALL_TIMEOUT_S
        while time.monotonic() < deadline and terminal is None:
            conn.pump_events(timeout_s=2.0)
            for event in conn.events:
                if event.name == "action_terminal" and event.action_id == action_id:
                    terminal = event.data
                    break
        attempt["terminal"] = terminal
        attempt["events"] = [
            {"seq": e.seq, "tick": e.tick, "event": e.name, "data": e.data}
            for e in conn.events
            if e.action_id == action_id
        ]
        query = conn.request(conn.protocol.query_action(action_id))
        if isinstance(query, Response):
            attempt["query_reconciliation"] = {
                "state": query.payload.get("state"),
                "terminal": query.payload.get("terminal"),
                "terminal_matches_event": query.payload.get("terminal") == terminal,
            }
        attempts.append(attempt)
        if terminal is not None and terminal.get("classification") == "completed":
            completed = attempt
            break

    replay = conn.request(conn.protocol.resume_events(BOT_NAME, 0))
    artifact["navigate"] = {
        "attempts": attempts,
        "completed": completed is not None,
        "event_gaps": [{"from": g.from_seq, "to": g.to_seq} for g in conn.gaps],
        "replay_last_seq": replay.payload.get("last_seq") if isinstance(replay, Response) else None,
        "live_events_received": len(conn.events),
    }
    artifact["elapsed_wall_s"] = round(time.monotonic() - started, 3)
    write_artifact(artifact)
    return 0


def candidate_targets(conn: ProbeConnection) -> list[dict]:
    result = conn.request(
        conn.protocol.find_blocks(BOT_NAME, LOG_BLOCK_IDS, 128, vertical_radius=16, limit=64)
    )
    if isinstance(result, ErrorResponse):
        return []
    matches = list(result.payload.get("matches", []))
    # One candidate per tree: keep matches at least 4 blocks apart in XZ.
    spread: list[dict] = []
    for match in matches:
        if all(abs(match["x"] - kept["x"]) + abs(match["z"] - kept["z"]) >= 4 for kept in spread):
            spread.append(match)
    return spread


def write_artifact(artifact: dict) -> None:
    out = Path("logs/agentic-runtime/java-body-m1-live-probe-20260726.json")
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    sys.exit(main())
