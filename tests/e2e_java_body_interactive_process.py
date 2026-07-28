"""Bounded Java-only production-process chat preflight. Not a formal gate.

The production child receives no RCON configuration. This parent process uses
RCON only as an external fixture to create FakePlayers and send public chat.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from minebot.game import RconClient
from minebot.game.rcon import RconConfig


BOT = "JavaProcessBot"
GUIDE = "JavaProcGuide"
BODY_URL = "ws://127.0.0.1:8767"
PUBLIC_GOAL = "collect 1 dirt"
PUBLIC_GOAL_COMMAND = f"/goal {PUBLIC_GOAL}"
RCON_ENV_KEYS = (
    "MINEBOT_REAL_RCON_HOST",
    "MINEBOT_REAL_RCON_PORT",
    "MINEBOT_REAL_RCON_PASSWORD",
    "MINEBOT_REAL_RCON_TIMEOUT",
)


def wait_for_player(rcon: RconClient, name: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "No entity was found" not in rcon.command(f"data get entity {name} Pos"):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{name} did not join the test world")


def read_trace(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def wait_for_record(path: Path, predicate, *, timeout_s: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for record in read_trace(path):
            if predicate(record):
                return record
        time.sleep(0.1)
    raise TimeoutError(f"expected production trace record did not arrive: {path}")


def wait_for_ready(path: Path, process: subprocess.Popen, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"production process exited before ready: {process.returncode}")
        if path.exists() and "interactive_ready" in path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return
        time.sleep(0.1)
    raise TimeoutError("production process did not become interactive-ready")


def stop_process(process: subprocess.Popen) -> int | None:
    if process.poll() is not None:
        return process.returncode
    process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def main() -> int:
    run_dir = Path("logs/agentic-runtime/java-body-interactive-process-20260728")
    run_dir.mkdir(parents=True, exist_ok=True)
    trace_path = run_dir / "trace.jsonl"
    stdout_path = run_dir / "process.log"
    state_path = run_dir / "state.sqlite3"
    artifact_path = Path(
        "logs/agentic-runtime/java-body-interactive-process-20260728.json"
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
            "MINEBOT_BODY_PROVIDER": "java",
            "MINEBOT_JAVA_BODY_URL": BODY_URL,
            "MINEBOT_REAL_BOT": BOT,
            "MINEBOT_REAL_NATURAL_REGION": "-128,-64,-128,128,320,128",
            "MINEBOT_AGENT_LOG_PATH": str(trace_path),
            "MINEBOT_AGENT_STATE_DB": str(state_path),
        }
    )
    for key in RCON_ENV_KEYS:
        child_env.pop(key, None)

    artifact: dict[str, object] = {
        "scope": "java_body_interactive_production_process",
        "formal_gate": False,
        "bounded": True,
        "body_provider": "java",
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
                rcon.command(f"execute as {GUIDE} run me /quit java_preflight_complete")
                try:
                    process_exit_code = process.wait(timeout=45.0)
                except subprocess.TimeoutExpired:
                    process_exit_code = stop_process(process)

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
            }
        )
        success = (
            artifact["production_rcon_env_present"] is False
            and artifact["manifest_body_provider"] == "java"
            and artifact["legacy_rcon_constructed"] is False
            and artifact["legacy_scarpet_body_constructed"] is False
            and artifact["chat_command"] == "replace_goal"
            and artifact["chat_sender"] == GUIDE
            and artifact["chat_content"] == PUBLIC_GOAL
            and artifact["world_id_present"] is True
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
