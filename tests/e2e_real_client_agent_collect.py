"""Real-model collection and optional continuous-task acceptance in a disposable world.

The production Agent receives user goals and messages, not a prescribed tool
sequence. Parent-only RCON prepares fixtures and independently checks results.
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


def run_continuity(args, environment, rcon, bot, goal, report, log):
    """Drive user messages only; the real model chooses every physical tool."""
    trace_path = args.output_dir / "trace.jsonl"
    deadline = time.monotonic() + args.timeout
    process = subprocess.Popen(
        [sys.executable, "-m", "minebot.app.real_server_session", goal, "--interactive", "--max-steps", "30"],
        env=environment, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8",
    )
    report["messages"] = []
    report["minecraft_task_started"] = True

    def events():
        records = []
        if trace_path.exists():
            for line in trace_path.read_text().splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # The producer can be partway through the last line.
        return records

    def wait(label, predicate):
        report["phase"] = label
        while time.monotonic() < deadline:
            records = events()
            match = predicate(records)
            if match:
                return match
            failure = next((e for e in reversed(records) if e.get("event") == "responses_failed"
                            or (e.get("event") == "task_outcome" and e.get("status") == "blocked")), None)
            if failure:
                raise RuntimeError(f"{label}: {failure}")
            if process.poll() is not None:
                raise RuntimeError(f"{label}: Agent exited {process.returncode}; see agent.log")
            time.sleep(0.05)
        raise RuntimeError(f"{label}: exceeded {args.timeout:g}s; see trace.jsonl")

    def send(text):
        report["messages"].append({"text": text, "trace_count_before": len(events())})
        process.stdin.write(text + "\n")
        process.stdin.flush()

    def pending_tool(records, after, predicate):
        completed = {e.get("tool_call_id") for e in records if e.get("event") == "tool_result"}
        return next((e for e in records[after:] if e.get("event") == "tool_invoke"
                     and e.get("tool_call_id") not in completed and predicate(e)), None)

    def position():
        raw = rcon.command(f"data get entity {bot} Pos")
        report.setdefault("server_positions", []).append(raw)
        values = re.search(r"\[([^]]+)\]", raw)
        if values is None:
            raise RuntimeError(f"Server position unavailable: {raw}")
        return tuple(float(v.strip().rstrip("dDfF")) for v in values[1].split(","))

    try:
        def collecting_active(es):
            call = pending_tool(es, 0, lambda e: "collect" in e.get("tool", ""))
            if call and sum((a - b) ** 2 for a, b in zip(position(), (32.5, 150, 28.5))) > 0.25:
                return call
            return None
        collecting = wait("collecting_before_chat", collecting_active)
        report["collect_call_id"] = collecting["tool_call_id"]
        report["native_async_call"] = next((e for e in events()
            if e.get("event") == "responses_tool_started" and e.get("tool_call_id") == collecting["tool_call_id"]), None)
        if not report["native_async_call"] or not report["native_async_call"].get("asynchronous"):
            raise RuntimeError("The actual collection call was not native async")
        chat_cursor = len(events())
        send("How is it going? Keep doing the job.")
        send("After giving the logs to Receiver, return to our starting point (32.5, 150, 28.5).")
        receipt = wait("collect_result_after_chat", lambda es: next((e for e in es[chat_cursor:]
            if e.get("event") == "tool_result" and e.get("tool_call_id") == collecting["tool_call_id"]), None))
        report["collect_result"] = receipt
        if not receipt.get("success"):
            raise RuntimeError(f"Collection failed: {receipt.get('reason')}")
        if any(e.get("event") == "execution_cancelled" for e in events()[chat_cursor:]):
            raise RuntimeError("Physical work was cancelled while handling ordinary chat")
        report["late_result_ack"] = wait("automatic_result_continuation", lambda es: next((e for e in es
            if e.get("event") == "responses_tool_output" and e.get("tool_call_id") == collecting["tool_call_id"]), None))
        report["task_outcome"] = wait("handoff_and_return", lambda es: next((e for e in es
            if e.get("event") == "task_outcome" and e.get("status") == "complete"), None))
        observed = rcon.command("clear Receiver minecraft:oak_log 0")
        report["receiver_inventory"] = rcon.command("data get entity Receiver Inventory")
        report["receiver_count"] = observed
        count = re.search(r"Found (\d+)\b", observed)
        if count is None or int(count[1]) < 3:
            raise RuntimeError(f"Receiver did not receive three logs: {observed}")
        returned = position()
        if sum((a - b) ** 2 for a, b in zip(returned, (32.5, 150, 28.5))) > 4:
            raise RuntimeError(f"Appended return request was not fulfilled: {returned}")
        report["handoff_and_return_success"] = True

        cancel_cursor = len(events())
        send("/goal Walk to (52.5, 150, 28.5).")
        moving = wait("movement_before_cancel", lambda es: pending_tool(
            es, cancel_cursor, lambda e: "navigation" in e.get("body_scope", [])
            or any(term in e.get("tool", "") for term in ("navigate", "move", "goto"))))
        report["cancel_call_id"] = moving["tool_call_id"]
        def movement_observed(es):
            current = position()
            if any(e.get("event") == "tool_result" and e.get("tool_call_id") == moving["tool_call_id"] for e in es):
                raise RuntimeError("Movement finished before the cancellation intervention")
            return current if (sum((a - b) ** 2 for a, b in zip(current, returned)) > 0.25
                               and sum((a - b) ** 2 for a, b in zip(current, (52.5, 150, 28.5))) > 4) else None
        report["moving_server_position"] = wait("actual_movement_before_cancel", movement_observed)
        before_cancel = len(events())
        send("/cancel experiment explicit cancellation")
        report["cancel_receipt"] = wait("physical_cancel_settlement", lambda es: next((e for e in es[before_cancel:]
            if e.get("event") == "execution_cancelled" and e.get("settled")), None))
        report["idle_after_cancel"] = wait("idle_after_cancel", lambda es: next((e for e in es[before_cancel:]
            if e.get("event") == "body_state" and e.get("pending_action_count") == 0), None))
        stopped = position()
        time.sleep(1)
        later = position()
        if sum((a - b) ** 2 for a, b in zip(stopped, later)) > 0.04:
            raise RuntimeError(f"Old movement continued after cancellation: {stopped} -> {later}")
        report["cancel_success"] = True
        report.update(success=True, phase="completed", reason="Collection, handoff, appended return and explicit cancellation passed")
        return 0
    finally:
        if process.poll() is None:
            try:
                send("/quit experiment ended")
                process.wait(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        process.stdin.close()
        report["agent_exit_code"] = process.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--continuity", action="store_true", help="Native Responses collection/handoff, chat, appended return and explicit cancel")
    parser.add_argument("--model", help="Optional experiment model override")
    parser.add_argument("--transport", choices=("http", "websocket"), default="http")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output-dir", type=Path, default=Path("logs") / f"agent-collect-{time.time_ns()}")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    bot = "MineBotCombat"
    goal = ("Collect 3 oak logs and give them to Receiver at (52.5, 150, 28.5). "
            "Our starting point is (32.5, 150, 28.5).") if args.continuity else "collect 3 logs"
    fixture_started = False
    started = time.monotonic()
    body = RealClientBody(bot, RealClientIpc(timeout_s=30))
    rcon = RconClient(RconConfig(password=os.environ.get("MINEBOT_TEST_RCON_PASSWORD", "test")))
    report = {"goal": goal, "evidence": "real_model_agent_task", "success": False, "minecraft_task_started": False,
              "conditions": "empty inventory; three placed logs and a stone obstacle; no supplied tools; no human action after goal",
              "tuple": "MC 26.1.2 / Fabric 0.19.3 / API 0.152.1 / Java 25 / Baritone 1.18.0"}
    try:
        environment = load_runtime_environment(args.env_file)
        if args.model:
            environment["MINEBOT_LLM_MODEL"] = args.model
        if args.continuity:
            environment.update(MINEBOT_AGENT_KERNEL="responses", MINEBOT_LLM_KIND="openai_responses", MINEBOT_RESPONSES_TRANSPORT=args.transport)
            report["transport"] = args.transport
            report["conditions"] = "empty inventories; three logs behind a stone obstacle; Receiver at far end; user chat and appended return during collection; separate movement cancellation"
            receiver = RealClientBody("Receiver", RealClientIpc(port=8769, timeout_s=5))
            try:
                receiver.get_state()
            finally:
                receiver.close()
        fixture_started = True
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
        if args.continuity:
            rcon.command("gamemode survival Receiver")
            rcon.command("clear Receiver")
            rcon.command("tp Receiver 52.5 150 28.5")
        report["seed"] = rcon.command("seed")
        report["initial_state"] = body.ipc.state()
        body.close()  # The IPC endpoint has one controller; hand it to the Agent.
        environment = {k: v for k, v in environment.items() if "RCON" not in k.upper()}
        environment.update({
            "MINEBOT_BODY_PROVIDER": "real_client", "MINEBOT_REAL_BOT": bot,
            "MINEBOT_REAL_CLIENT_HOST": "127.0.0.1", "MINEBOT_REAL_CLIENT_PORT": "8768",
            "MINEBOT_REAL_NATURAL_REGION": "32,149,24,56,156,34",
            "MINEBOT_AGENT_LOG_PATH": str(args.output_dir / "trace.jsonl"),
            "MINEBOT_AGENT_STATE_DB": str(args.output_dir / "state.sqlite3"),
        })
        with (args.output_dir / "agent.log").open("w", encoding="utf-8") as log:
            if args.continuity:
                return run_continuity(args, environment, rcon, bot, goal, report, log)
            report["minecraft_task_started"] = True
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
            if fixture_started:
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
