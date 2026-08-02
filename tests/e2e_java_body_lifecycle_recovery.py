"""Bounded live proof for Java action deadline and event-stream recovery."""

from __future__ import annotations

import json
import time
from pathlib import Path

from minebot.game import RconClient
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import JavaBodyClient, websocket_transport
from minebot.game.rcon import RconConfig


BOT = "JavaLifeProbe"
BODY_URL = "ws://127.0.0.1:8767"
ARTIFACT = Path("logs/agentic-runtime/java-body-lifecycle-recovery-20260802.json")


def main() -> int:
    connect = websocket_transport(BODY_URL)
    read_client = JavaBodyClient(BOT, connect, subscribe_events=False)
    action_client = JavaBodyClient(
        BOT,
        connect,
        action_wall_timeout_s=0.5,
        recv_timeout_s=0.5,
        terminal_query_interval_s=0.2,
        terminal_delivery_grace_s=3.0,
    )
    action_client.configure_reconciliation_client(read_client)
    body = JavaBody(action_client, BOT, read_client=read_client)
    artifact: dict[str, object] = {
        "scope": "java_body_lifecycle_recovery",
        "formal_gate": False,
        "bounded": True,
    }
    rcon = RconClient(
        RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
    )

    try:
        rcon.connect()
        rcon.command(f"player {BOT} kill")
        rcon.command("fill 290 70 290 350 73 310 air")
        rcon.command("fill 290 69 290 350 69 310 stone")
        rcon.command("forceload add 288 288 352 320")
        rcon.command(f"player {BOT} spawn at 300.5 70 300.5")
        rcon.command(f"gamemode survival {BOT}")
        time.sleep(2.0)
        initial_state = body.get_state()
        if abs(initial_state.pos[0] - 300.5) > 1.0 or abs(initial_state.pos[2] - 300.5) > 1.0:
            raise RuntimeError(f"fixture spawn did not settle on platform: {initial_state.pos}")

        rcon.command("tick rate 1")
        try:
            deadline_result = action_client.navigate(
                {"kind": "xz", "x": 340, "z": 300},
                timeout_ticks=40,
            )
        finally:
            rcon.command("tick rate 20")

        deadline_state = body.get_state()
        baseline_events = body.poll_events()
        baseline_seq = body.last_seq

        action_client.close()
        actor = JavaBodyClient(BOT, connect, subscribe_events=False)
        try:
            position = body.get_state().pos
            replay_source = actor.navigate(
                {
                    "kind": "xz",
                    "x": round(position[0]),
                    "z": round(position[2]),
                },
                timeout_ticks=100,
            )
            replay_action_id = (
                None
                if actor.last_action_terminal is None
                else actor.last_action_terminal[0]
            )
        finally:
            actor.close()

        replayed = body.poll_events()
        replay_terminal = next(
            (
                event
                for event in replayed
                if event.name == "action_terminal"
                and event.data.get("action_id") == replay_action_id
            ),
            None,
        )

        action_client.close()
        cleanup_head = body.event_head("lifecycle-recovery-cleanup")
        artifact.update(
            {
                "deadline_result": {
                    "success": deadline_result.success,
                    "reason": deadline_result.reason,
                    "can_retry": deadline_result.can_retry,
                    "metrics": dict(deadline_result.metrics or {}),
                },
                "deadline_owner": deadline_state.body_owner,
                "deadline_pending_action_count": deadline_state.pending_action_count,
                "baseline_event_count": len(baseline_events),
                "baseline_seq": baseline_seq,
                "replay_source_success": replay_source.success,
                "replay_source_reason": replay_source.reason,
                "replay_action_id": replay_action_id,
                "replayed_event_count": len(replayed),
                "replay_terminal_observed": replay_terminal is not None,
                "replay_terminal_seq": None if replay_terminal is None else replay_terminal.seq,
                "cleanup_owner": cleanup_head.get("owner"),
                "cleanup_pending_action_count": cleanup_head.get("pending_action_count"),
            }
        )
        artifact["success"] = (
            deadline_result.success is False
            and deadline_result.reason == "cancel_requested"
            and deadline_result.metrics.get("deadline_cancel_requested") is True
            and deadline_state.body_owner is None
            and int(deadline_state.pending_action_count or 0) == 0
            and replay_source.success is True
            and replay_terminal is not None
            and replay_terminal.seq > baseline_seq
            and cleanup_head.get("owner") is None
            and int(cleanup_head.get("pending_action_count") or 0) == 0
        )
    except Exception as exc:
        artifact.update(
            {
                "success": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
    finally:
        try:
            rcon.command("tick rate 20")
            rcon.command("forceload remove 288 288 352 320")
            rcon.command(f"player {BOT} kill")
        except Exception:
            artifact["fixture_cleanup_complete"] = False
        else:
            artifact["fixture_cleanup_complete"] = True
        action_client.close()
        read_client.close()
        rcon.close()

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
