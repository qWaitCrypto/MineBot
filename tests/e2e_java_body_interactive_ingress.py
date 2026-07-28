"""Bounded Java-only world identity and chat surface probe. Not a formal gate.

RCON creates the disposable FakePlayer and independently reads the persisted
world marker. Production identity, chat polling, and speech use JavaBody only.
"""

from __future__ import annotations

import json
from pathlib import Path
import time

from minebot.app.body_provider import build_body_provider
from minebot.app.runtime_identity import parse_world_identity_response
from minebot.contract import Region
from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "JavaIngressProbe"
GUIDE = "JavaIngressGuide"
BODY_URL = "ws://127.0.0.1:8767"
REGION = Region("interactive-probe-natural", (-128, -64, -128), (128, 320, 128))
SPEECH = "java interactive ingress ready"
PUBLIC_GOAL = "/goal collect one log"


def wait_for_player(rcon: RconClient, name: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "No entity was found" not in rcon.command(f"data get entity {name} Pos"):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{name} did not join the test world")


def wait_for_speech_log() -> bool:
    log = Path("test-server/logs/latest.log")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if log.exists() and SPEECH in log.read_text(encoding="utf-8", errors="replace"):
            return True
        time.sleep(0.1)
    return False


def wait_for_public_goal(body) -> list:
    deadline = time.monotonic() + 5.0
    events = []
    while time.monotonic() < deadline:
        events.extend(body.poll_chat_events())
        if any(
            event.name == "agentChat"
            and event.data.get("sender") == GUIDE
            and event.data.get("message") == PUBLIC_GOAL
            for event in events
        ):
            return events
        time.sleep(0.1)
    return events


def main() -> int:
    artifact: dict[str, object] = {
        "scope": "java_body_interactive_ingress",
        "formal_gate": False,
        "bounded": True,
        "body_provider": "java",
        "scarpet_body_constructed": False,
        "rcon_role": "fixture_spawn_and_independent_world_marker_read_only",
    }
    with RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    ) as rcon:
        rcon.command(f"player {BOT} spawn")
        rcon.command(f"player {GUIDE} spawn")
        wait_for_player(rcon, BOT)
        wait_for_player(rcon, GUIDE)
        runtime = build_body_provider(
            "java",
            bot_name=BOT,
            natural_region=REGION,
            java_body_url=BODY_URL,
        )
        body = runtime.body
        try:
            head = body.event_head("bounded-interactive-probe")
            rcon.command(f"execute as {GUIDE} run me {PUBLIC_GOAL}")
            chat_events = wait_for_public_goal(body)
            world_id_first = body.world_identity()
            world_id_second = body.world_identity()
            persisted = parse_world_identity_response(
                rcon.command("data get storage minebot:runtime world_id")
            )
            said = body.say(SPEECH)
            speech_logged = wait_for_speech_log()
        finally:
            body.interrupt("bounded_probe_cleanup")
            rcon.command(f"player {GUIDE} kill")
            rcon.command(f"player {BOT} kill")

    public_goal_received = any(
        event.name == "agentChat"
        and event.data.get("sender") == GUIDE
        and event.data.get("message") == PUBLIC_GOAL
        for event in chat_events
    )

    artifact.update(
        {
            "event_head": head,
            "world_id_first": world_id_first,
            "world_id_second": world_id_second,
            "persisted_world_id": persisted,
            "chat_event_count": len(chat_events),
            "public_goal_received": public_goal_received,
            "public_goal_sender": GUIDE,
            "public_goal_text": PUBLIC_GOAL,
            "said": said,
            "speech_logged": speech_logged,
        }
    )
    success = (
        bool(world_id_first)
        and world_id_first == world_id_second == persisted
        and str(head.get("epoch") or "") != ""
        and int(head.get("chat_seq") or 0) == 0
        and public_goal_received
        and said
        and speech_logged
    )
    artifact["success"] = success
    out = Path("logs/agentic-runtime/java-body-interactive-ingress-20260728.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
