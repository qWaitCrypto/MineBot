"""One real-model collection task in a disposable loopback world.

The production Agent receives a goal, not a prescribed tool sequence. RCON
prepares the fixture and independently checks inventory in the parent only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from minebot.app.local_launcher import load_runtime_environment
from minebot.game.rcon import RconClient, RconConfig
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output-dir", type=Path, default=Path("logs") / f"agent-collect-{time.time_ns()}")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    bot = "MineBotCombat"
    goal = "collect 3 logs"
    started = time.monotonic()
    body = RealClientBody(bot, RealClientIpc(timeout_s=30))
    rcon = RconClient(RconConfig(password=os.environ.get("MINEBOT_TEST_RCON_PASSWORD", "test")))
    report = {"goal": goal, "evidence": "real_model_agent_task", "success": False,
              "conditions": "empty inventory; three placed logs and a stone obstacle; no supplied tools; no human action after goal",
              "tuple": "MC 26.1.2 / Fabric 0.19.3 / API 0.152.1 / Java 25 / Baritone 1.18.0"}
    try:
        body.get_state()
        rcon.command("fill 32 149 24 56 149 34 minecraft:stone")
        rcon.command("fill 32 150 24 56 155 34 minecraft:air")
        rcon.command("fill 39 150 27 39 152 29 minecraft:stone")
        rcon.command("fill 44 150 28 44 152 28 minecraft:oak_log")
        rcon.command(f"gamemode survival {bot}")
        rcon.command(f"clear {bot}")
        rcon.command(f"tp {bot} 32.5 150 28.5")
        deadline = time.monotonic() + 5
        while body.get_state().pos != (32.5, 150.0, 28.5):
            if time.monotonic() >= deadline:
                raise RuntimeError("client did not observe fixture teleport")
            time.sleep(0.1)
        report["seed"] = rcon.command("seed")
        report["initial_state"] = body.ipc.state()
        body.close()  # The IPC endpoint has one controller; hand it to the Agent.
        environment = load_runtime_environment(args.env_file)
        environment = {k: v for k, v in environment.items() if "RCON" not in k.upper()}
        environment.update({
            "MINEBOT_BODY_PROVIDER": "real_client", "MINEBOT_REAL_BOT": bot,
            "MINEBOT_REAL_CLIENT_HOST": "127.0.0.1", "MINEBOT_REAL_CLIENT_PORT": "8768",
            "MINEBOT_REAL_NATURAL_REGION": "32,149,24,56,156,34",
            "MINEBOT_AGENT_LOG_PATH": str(args.output_dir / "trace.jsonl"),
            "MINEBOT_AGENT_STATE_DB": str(args.output_dir / "state.sqlite3"),
        })
        with (args.output_dir / "agent.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(
                [sys.executable, "-m", "minebot.app.real_server_session", goal, "--max-steps", "20"],
                env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout,
            )
        report["exit_code"] = result.returncode
        report["server_inventory"] = rcon.command(f"data get entity {bot} Inventory")
        observed = rcon.command(f"clear {bot} minecraft:oak_log 0")
        report["server_count"] = observed
        # `clear ... 0` counts across all inventory slots without removing items.
        count = re.search(r"Found (\d+)\b", observed)
        report["success"] = result.returncode == 0 and count is not None and int(count[1]) >= 3
        report["reason"] = "goal and server inventory passed" if report["success"] else (
            f"Agent exit={result.returncode}; server inventory count: {observed}; see agent.log and trace.jsonl")
        trace = args.output_dir / "trace.jsonl"
        if not report["success"] and trace.exists():
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            failures = [event for event in events if event.get("event") in {
                "session_step_failed", "model_transport_recovery_exhausted"}]
            if failures:
                failure = failures[-1]
                report["reason"] = (
                    f"Agent failed: {failure.get('error_type')}; "
                    f"{failure.get('error_message') or failure['event']}; inventory: {observed}")
    except subprocess.TimeoutExpired:
        report["reason"] = f"Agent exceeded {args.timeout:g}s; see agent.log and trace.jsonl"
    except Exception as error:
        report["reason"] = f"{type(error).__name__}: {error}"
    finally:
        report["elapsed_s"] = round(time.monotonic() - started, 3)
        try:
            body.get_state()
            report["final_state"] = body.ipc.state()
            if report["final_state"].get("any_process_active"):
                body.interrupt("experiment ended")
        except Exception as error:
            report["final_state_error"] = f"{type(error).__name__}: {error}"
        body.close()
        rcon.close()
        path = args.output_dir / "result.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"{'PASS' if report['success'] else 'FAIL'} Agent collection: {report['reason']}")
        print(f"Evidence: {path}")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
