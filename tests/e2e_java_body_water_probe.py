"""Live evidence for M3 liquid traversal over naturally occurring water.

The golden spawn is ringed by natural water; before M3 every tree across it
returned no_path. This probe spawns at the natural spawn, finds the nearest
log across the water, and navigates to it — no injected blocks, no disturbance.
Success is a completed navigation whose executed path physically crosses water
(the bot's Y or a swim event shows it entered the liquid), proving the water
gate is open. Any natural replan/stuck escalation is recorded as JB-12
evidence. formal_gate: false.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from websockets.sync.client import connect

from minebot.game.java_body_protocol import BotEvent, ErrorResponse, JavaBodyProtocol, Response
from minebot.game.rcon import RconClient, RconConfig

BOT = "JavaBodyProbe"
LOG_FAMILY = [
    "minecraft:oak_log",
    "minecraft:spruce_log",
    "minecraft:birch_log",
    "minecraft:dark_oak_log",
]


class Probe:
    def __init__(self) -> None:
        self.socket = connect("ws://127.0.0.1:8767", open_timeout=10)
        self.protocol = JavaBodyProtocol()
        self.events: list[BotEvent] = []

    def request(self, message: dict, timeout_s: float = 15.0):
        self.socket.send(json.dumps(message))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for item in self.protocol.feed(json.loads(self.socket.recv(timeout=deadline - time.monotonic()))):
                if isinstance(item, (Response, ErrorResponse)):
                    return item
                if isinstance(item, BotEvent):
                    self.events.append(item)
        raise TimeoutError(message["type"])

    def wait_terminal(self, action_id: str, timeout_s: float, sampler=None) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                for item in self.protocol.feed(json.loads(self.socket.recv(timeout=1.0))):
                    if isinstance(item, BotEvent):
                        self.events.append(item)
                        if item.name == "action_terminal" and item.action_id == action_id:
                            return item.data
            except TimeoutError:
                pass
            if sampler is not None:
                sampler()
        return None

    def event_names(self, action_id: str) -> list[str]:
        return [e.name for e in self.events if e.action_id == action_id]


def pos(rcon: RconClient) -> list[float]:
    raw = rcon.command(f"data get entity {BOT} Pos")
    return [float(v) for v in re.findall(r"(-?\d+\.?\d*)d", raw)]


def in_water(rcon: RconClient) -> bool:
    # Authoritative: the block at the bot's feet is water.
    p = pos(rcon)
    fx, fy, fz = int(p[0]), int(p[1]), int(p[2])
    block = rcon.command(f"execute if block {fx} {fy} {fz} water run data get entity {BOT} UUID")
    return "entity data" in block


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    rcon.command(f"player {BOT} spawn")
    time.sleep(2)
    rcon.command(f"gamemode survival {BOT}")
    # Drop between the natural spawn water and the tree cluster the M1 probe
    # found water-gated, so the shortest route to a log crosses real water.
    rcon.command(f"tp {BOT} 30 80 -28")
    time.sleep(5)
    start = pos(rcon)

    probe = Probe()
    probe.request(probe.protocol.hello())

    search = probe.request(probe.protocol.find_blocks(BOT, LOG_FAMILY, 128, vertical_radius=48, limit=64))
    matches = search.payload.get("matches", []) if isinstance(search, Response) else []

    artifact: dict = {
        "scope": "java_body_m3_liquid_live_probe",
        "formal_gate": False,
        "bounded": True,
        "start_pos": start,
        "candidates_found": len(matches),
        "attempts": [],
    }

    crossed = None
    for target in matches[:6]:
        action = f"m3-swim-{int(time.time())}"
        goal = {"kind": "near", "x": target["x"], "y": target["y"], "z": target["z"], "range": 3.0}
        ack = probe.request(probe.protocol.navigate(BOT, action, goal, timeout_ticks=6000))
        entered_water = {"v": False}
        min_y = {"v": start[1]}

        def sample():
            p = pos(rcon)
            min_y["v"] = min(min_y["v"], p[1])
            if not entered_water["v"] and in_water(rcon):
                entered_water["v"] = True

        terminal = probe.wait_terminal(action, 90, sampler=sample)
        end = pos(rcon)
        attempt = {
            "target": target,
            "terminal": terminal,
            "entered_water": entered_water["v"],
            "min_y": round(min_y["v"], 2),
            "displacement": round(((end[0] - start[0]) ** 2 + (end[2] - start[2]) ** 2) ** 0.5, 2),
            "replans": probe.event_names(action).count("replan_started"),
            "path_plans": probe.event_names(action).count("path_planned"),
        }
        artifact["attempts"].append(attempt)
        if terminal and terminal.get("classification") == "completed" and entered_water["v"]:
            crossed = attempt
            break
        if terminal and terminal.get("classification") == "completed":
            # Reached a target without touching water; keep trying for a
            # genuinely water-gated one, but record the success.
            crossed = crossed or attempt

    artifact["water_crossing_proven"] = bool(crossed and crossed.get("entered_water"))
    artifact["any_completion"] = bool(crossed)
    artifact["total_replans"] = sum(a["replans"] for a in artifact["attempts"])

    out = Path("logs/agentic-runtime/java-body-m3-liquid-live-probe-20260727.json")
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
