"""Bounded live behavior evidence for the Mindcraft comparison. Not a gate.

Three natural-world cases: an absent target must be a typed not_found with
coverage facts; a mid-flight cancellation must stop the body and terminate
as canceled; a long route must either complete or fail with typed facts,
recording any naturally occurring replans.
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

    def wait_terminal(self, action_id: str, timeout_s: float) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                for item in self.protocol.feed(json.loads(self.socket.recv(timeout=2.0))):
                    if isinstance(item, BotEvent):
                        self.events.append(item)
            except TimeoutError:
                pass
            for event in self.events:
                if event.name == "action_terminal" and event.action_id == action_id:
                    return event.data
        return None

    def action_event_names(self, action_id: str) -> list[str]:
        return [e.name for e in self.events if e.action_id == action_id]


def bot_pos(rcon: RconClient) -> list[float]:
    raw = rcon.command(f"data get entity {BOT} Pos")
    return [float(v) for v in re.findall(r"(-?\d+\.?\d*)d", raw)]


def main() -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=25576, password="test"))
    rcon.connect()
    rcon.command(f"player {BOT} spawn")
    time.sleep(2)
    rcon.command(f"gamemode survival {BOT}")

    probe = Probe()
    probe.request(probe.protocol.hello())
    artifact: dict = {"scope": "java_body_behavior_probe", "formal_gate": False, "bounded": True, "cases": {}}
    stamp = int(time.time())

    # 1. Absent target: typed not_found with coverage facts, no wandering.
    action = f"bx-notfound-{stamp}"
    probe.request(probe.protocol.collect_block(BOT, action, ["minecraft:diamond_block"], radius=16, timeout_ticks=600))
    terminal = probe.wait_terminal(action, 40)
    artifact["cases"]["absent_target"] = {
        "terminal": terminal,
        "events": probe.action_event_names(action),
    }

    # 2. Mid-flight cancellation: canceled terminal and a stopped body.
    # Reset to the spawn plain, where eastward walking is proven ground.
    rcon.command(f"tp {BOT} 0 90 0")
    time.sleep(4)
    action = f"bx-cancel-{stamp}"
    start = bot_pos(rcon)
    goal = {"kind": "xz", "x": int(start[0]) + 40, "z": int(start[2])}
    ack = probe.request(probe.protocol.navigate(BOT, action, goal, timeout_ticks=2400))
    time.sleep(2.5)
    cancel = probe.request(probe.protocol.cancel_action(action))
    terminal = probe.wait_terminal(action, 30)
    pos_after = bot_pos(rcon)
    time.sleep(2)
    pos_settled = bot_pos(rcon)
    drift = sum(abs(a - b) for a, b in zip(pos_after, pos_settled))
    artifact["cases"]["cancel_mid_flight"] = {
        "ack": ack.payload.get("state") if isinstance(ack, Response) else ack.payload,
        "cancel_state": cancel.payload.get("state") if isinstance(cancel, Response) else None,
        "terminal": terminal,
        "moved_before_cancel": sum(abs(a - b) for a, b in zip(start, pos_after)) > 1.0,
        "post_cancel_drift_blocks": round(drift, 3),
        "events": probe.action_event_names(action),
    }

    # 3. Long route: complete or typed failure; record natural replans.
    action = f"bx-longroute-{stamp}"
    start = bot_pos(rcon)
    goal = {"kind": "xz", "x": int(start[0]) + 45, "z": int(start[2]) - 35}
    probe.request(probe.protocol.navigate(BOT, action, goal, timeout_ticks=6000))
    terminal = probe.wait_terminal(action, 240)
    end = bot_pos(rcon)
    artifact["cases"]["long_route"] = {
        "goal": goal,
        "terminal": terminal,
        "displacement_blocks": round(((end[0] - start[0]) ** 2 + (end[2] - start[2]) ** 2) ** 0.5, 2),
        "replan_events": probe.action_event_names(action).count("replan_started"),
        "path_planned_events": probe.action_event_names(action).count("path_planned"),
    }

    out = Path("logs/agentic-runtime/java-body-behavior-probe-20260726.json")
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
