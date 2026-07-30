"""Bounded Java-only production quit while exploration owns the Body.

The production child has no RCON configuration. An external fixture sends a
public goal, waits until ``explore_for`` owns the Java Body, then sends public
``/quit``. Success requires a normal session terminal and released Body owner.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from e2e_java_body_interactive_process import (
    RCON_ENV_KEYS,
    read_trace,
    stop_process,
    wait_for_player,
    wait_for_ready,
    wait_for_record,
)
from minebot.game import RconClient
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import JavaBodyClient, websocket_transport
from minebot.game.rcon import RconConfig


BOT = "JavaQuitBot"
GUIDE = "JavaQuitGuide"
BODY_URL = "ws://127.0.0.1:8767"
PUBLIC_GOAL = "Explore new regions to find a blue orchid."
PUBLIC_GOAL_COMMAND = f"/goal {PUBLIC_GOAL}"


def main() -> int:
    run_dir = Path("logs/agentic-runtime/java-body-long-action-quit-20260730")
    run_dir.mkdir(parents=True, exist_ok=True)
    trace_path = run_dir / "trace.jsonl"
    stdout_path = run_dir / "process.log"
    state_path = run_dir / "state.sqlite3"
    artifact_path = Path(
        "logs/agentic-runtime/java-body-long-action-quit-20260730.json"
    )
    for path in (
        trace_path,
        stdout_path,
        state_path,
        Path(f"{state_path}-shm"),
        Path(f"{state_path}-wal"),
    ):
        if path.exists():
            path.unlink()

    child_env = dict(os.environ)
    child_env.update(
        {
            "MINEBOT_JAVA_BODY_URL": BODY_URL,
            "MINEBOT_REAL_BOT": BOT,
            "MINEBOT_REAL_NATURAL_REGION": "-128,-64,-128,128,320,128",
            "MINEBOT_AGENT_LOG_PATH": str(trace_path),
            "MINEBOT_AGENT_STATE_DB": str(state_path),
        }
    )
    for key in RCON_ENV_KEYS:
        child_env.pop(key, None)
    child_env.pop("MINEBOT_BODY_PROVIDER", None)

    artifact: dict[str, object] = {
        "scope": "java_body_long_action_quit",
        "formal_gate": False,
        "bounded": True,
        "body_provider": "java",
        "production_provider_env_present": "MINEBOT_BODY_PROVIDER" in child_env,
        "public_goal_sender": GUIDE,
        "public_goal_text": PUBLIC_GOAL_COMMAND,
        "production_rcon_env_present": any(key in child_env for key in RCON_ENV_KEYS),
        "rcon_role": "external_fixture_only",
        "trace_path": str(trace_path),
        "process_log_path": str(stdout_path),
    }

    process: subprocess.Popen | None = None
    try:
        with RconClient(
            RconConfig(host="127.0.0.1", port=25576, password="test", timeout_s=20)
        ) as rcon:
            rcon.command(f"player {BOT} kill")
            rcon.command(f"player {GUIDE} kill")
            rcon.command(f"player {BOT} spawn")
            rcon.command(f"player {GUIDE} spawn")
            wait_for_player(rcon, BOT)
            wait_for_player(rcon, GUIDE)

            with stdout_path.open("w", encoding="utf-8") as output:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "minebot.app.real_server_session",
                        "--interactive",
                    ],
                    cwd=Path.cwd(),
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                wait_for_ready(stdout_path, process, timeout_s=60.0)
                rcon.command(f"execute as {GUIDE} run me {PUBLIC_GOAL_COMMAND}")
                chat = wait_for_record(
                    trace_path,
                    lambda record: (
                        record.get("event") == "chat_message"
                        and record.get("command") == "replace_goal"
                        and record.get("sender") == GUIDE
                        and record.get("content") == PUBLIC_GOAL
                    ),
                    timeout_s=30.0,
                )
                manifest = wait_for_record(
                    trace_path,
                    lambda record: record.get("event") == "provider_manifest",
                    timeout_s=5.0,
                )
                scope = wait_for_record(
                    trace_path,
                    lambda record: record.get("event") == "runtime_scope",
                    timeout_s=5.0,
                )
                tool_invoke = wait_for_record(
                    trace_path,
                    lambda record: (
                        record.get("event") == "tool_invoke"
                        and record.get("tool") == "explore_for"
                    ),
                    timeout_s=90.0,
                )
                observer = JavaBody(
                    JavaBodyClient(BOT, websocket_transport(BODY_URL)),
                    BOT,
                )
                active_deadline = time.monotonic() + 15.0
                active_state = observer.get_state()
                while (
                    active_state.body_owner is None
                    or int(active_state.pending_action_count or 0) == 0
                ) and time.monotonic() < active_deadline:
                    time.sleep(0.1)
                    active_state = observer.get_state()
                if active_state.body_owner is None or int(active_state.pending_action_count or 0) == 0:
                    raise TimeoutError("explore_for never acquired the Java Body")

                quit_started = time.monotonic()
                rcon.command(f"execute as {GUIDE} run me /quit java_long_action_complete")
                try:
                    process_exit_code = process.wait(timeout=20.0)
                except subprocess.TimeoutExpired:
                    process_exit_code = stop_process(process)
                quit_wall_s = time.monotonic() - quit_started

            trace = read_trace(trace_path)
            quit_chat = next(
                (
                    record
                    for record in trace
                    if record.get("event") == "chat_message"
                    and record.get("command") == "quit"
                ),
                {},
            )
            cancellation = next(
                (
                    record
                    for record in reversed(trace)
                    if record.get("event") == "execution_cancelled"
                    and record.get("reason") == "java_long_action_complete"
                ),
                {},
            )
            terminal = next(
                (
                    record
                    for record in reversed(trace)
                    if record.get("event") == "session_terminal"
                ),
                {},
            )
            quarantined = any(
                record.get("event") == "session_execution_quarantined"
                for record in trace
            )
            final_state = observer.get_state()

            rcon.command(f"player {GUIDE} kill")
            rcon.command(f"player {BOT} kill")

        artifact.update(
            {
                "chat_command": chat.get("command"),
                "chat_reason": chat.get("reason"),
                "chat_sender": chat.get("sender"),
                "chat_content": chat.get("content"),
                "manifest_body_provider": manifest.get("body_provider"),
                "legacy_rcon_constructed": manifest.get("legacy_rcon_constructed"),
                "legacy_scarpet_body_constructed": manifest.get(
                    "legacy_scarpet_body_constructed"
                ),
                "world_id_present": bool(scope.get("world_id")),
                "process_exit_code": process_exit_code,
                "active_tool": tool_invoke.get("tool"),
                "active_body_owner": active_state.body_owner,
                "active_pending_action_count": active_state.pending_action_count,
                "quit_chat_sender": quit_chat.get("sender"),
                "quit_chat_reason": quit_chat.get("reason"),
                "cancellation_settled": cancellation.get("settled"),
                "cancellation_execution_idle": cancellation.get("execution_idle"),
                "terminal_status": terminal.get("status"),
                "terminal_lifecycle": terminal.get("lifecycle"),
                "quarantined": quarantined,
                "final_body_owner": final_state.body_owner,
                "final_pending_action_count": final_state.pending_action_count,
                "quit_wall_s": quit_wall_s,
            }
        )
        success = (
            artifact["production_rcon_env_present"] is False
            and artifact["production_provider_env_present"] is False
            and artifact["manifest_body_provider"] == "java"
            and artifact["legacy_rcon_constructed"] is False
            and artifact["legacy_scarpet_body_constructed"] is False
            and artifact["chat_command"] == "replace_goal"
            and artifact["chat_sender"] == GUIDE
            and artifact["chat_content"] == PUBLIC_GOAL
            and artifact["world_id_present"] is True
            and artifact["active_tool"] == "explore_for"
            and artifact["active_body_owner"] is not None
            and int(artifact["active_pending_action_count"] or 0) > 0
            and artifact["quit_chat_sender"] == GUIDE
            and artifact["quit_chat_reason"] == "java_long_action_complete"
            and artifact["cancellation_settled"] is True
            and artifact["cancellation_execution_idle"] is True
            and artifact["terminal_status"] == "quit"
            and artifact["terminal_lifecycle"] == "idle"
            and artifact["quarantined"] is False
            and artifact["final_body_owner"] is None
            and int(artifact["final_pending_action_count"] or 0) == 0
            and float(artifact["quit_wall_s"] or 999.0) < 20.0
            and process_exit_code in {0, 5}
        )
        artifact["success"] = success
    except Exception as exc:
        artifact.update(
            {
                "success": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
    finally:
        if process is not None and process.poll() is None:
            artifact["forced_process_exit_code"] = stop_process(process)
        try:
            with RconClient(
                RconConfig(
                    host="127.0.0.1",
                    port=25576,
                    password="test",
                    timeout_s=5,
                    reconnect_attempts=0,
                )
            ) as cleanup_rcon:
                cleanup_rcon.command(f"player {GUIDE} kill")
                cleanup_rcon.command(f"player {BOT} kill")
        except Exception:
            artifact["fixture_cleanup_complete"] = False
        else:
            artifact["fixture_cleanup_complete"] = True
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
