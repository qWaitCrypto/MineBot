#!/usr/bin/env python3
"""G2 deterministic driver proof: empty inventory -> collect 3 diamond."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.observability import JsonlObservationSink  # noqa: E402
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime, inventory_count  # noqa: E402
from minebot.app.real_server_session import _goal_driver, safe_evaluate_terminal_truth  # noqa: E402
from minebot.app.runner import RuntimeTrace  # noqa: E402
from minebot.app.session import AgentSession, SessionCommand  # noqa: E402
from minebot.contract import Region  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402
from tools.setup_w3_arena import ARENA_MAX, ARENA_MIN, SPAWN_POS, setup_w3_arena  # noqa: E402

BOT = "E2EDiamondLadder"
GOAL = "collect 3 diamond"
ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs" / "g2-diamond-ladder"
REGION = Region("w3-diamond-ladder", ARENA_MIN, ARENA_MAX)


def command(rcon: RconClient, command_text: str, delay: float = 0.05) -> str:
    out = rcon.command(command_text)
    if delay:
        time.sleep(delay)
    return out


def reset_world_or_skip() -> None:
    env = dict(os.environ)
    env.setdefault("MINEBOT_E2E_REQUIRED", "1")
    result = subprocess.run(
        [str(ROOT / "tools" / "reset-world.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=260,
    )
    if result.returncode != 0:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise AssertionError(f"reset-world failed with {result.returncode}:\n{result.stdout}")
        print(f"SKIP: reset-world failed with {result.returncode}:\n{result.stdout}")
        raise SystemExit(SKIP_EXIT_CODE)


def prepare_bot(rcon: RconClient, body: ScarpetBody) -> dict[str, object]:
    arena = setup_w3_arena(rcon)
    command(rcon, f"player {BOT} kill")
    spawn_or_fail(body, SPAWN_POS)
    command(rcon, f"tp {BOT} {SPAWN_POS[0]} {SPAWN_POS[1]} {SPAWN_POS[2]} -90 0")
    command(rcon, f"gamemode survival {BOT}")
    command(rcon, f"effect clear {BOT}")
    command(rcon, f"clear {BOT}")
    for slot in range(46):
        body.transport.request(f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
    command(rcon, "script in minebot run minebot_reset()")
    if inventory_count(body, "diamond") != 0:
        raise AssertionError("bot inventory was not cleared before G2 run")
    return arena


async def run_goal_once(run_id: str, log_path: Path) -> dict[str, object]:
    config = RconConfig()
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: local RCON unavailable at {config.host}:{config.port}: {type(exc).__name__}: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)

    with rcon:
        body = ScarpetBody(BOT, rcon)
        arena = prepare_bot(rcon, body)
        sink = JsonlObservationSink(log_path)

        def make_parts(goal_text: str):
            trace = RuntimeTrace(session_id=run_id, sink=sink)
            return build_phase1_agent_runtime(
                body=body,
                goal_text=goal_text,
                model_provider=None,
                config=Phase1RuntimeConfig(natural_region=REGION, recovery_respawn_pos=SPAWN_POS, recovery_gamemode="survival"),
                agent_name=f"MineBotG2DiamondLadder-{run_id}",
                language="Chinese",
                trace=trace,
            )

        session = AgentSession(make_parts, goal_driver=_goal_driver)
        session.submit(SessionCommand.start(GOAL))
        final = await session.run_until_waiting(
            max_steps=1,
            should_stop=lambda step: safe_evaluate_terminal_truth(body, GOAL, step, session=session).satisfied,
        )
        truth = safe_evaluate_terminal_truth(body, GOAL, final, session=session)
        if truth.satisfied:
            final = session.complete_current_goal("terminal_truth_satisfied")
            truth = safe_evaluate_terminal_truth(body, GOAL, final, session=session)
        if session.parts is None:
            raise AssertionError("session did not create runtime parts")
        session.parts.runtime.trace.emit(
            "session_terminal",
            status=final.status,
            lifecycle=final.lifecycle.value,
            terminal_truth=truth.to_trace(),
        )
        session.parts.runtime.trace.close()

    events = _read_jsonl(log_path)
    tools = _tool_sequence(events)
    model_events = [event for event in events if _is_model_event(event)]
    diamond_count = int(truth.inventory_count or 0)
    if model_events:
        raise AssertionError(f"{run_id}: expected zero model events, got {[event.get('event') for event in model_events]}")
    if truth.exit_code != 0 or not truth.satisfied or diamond_count < 3:
        raise AssertionError(
            f"{run_id}: goal not satisfied exit={truth.exit_code} count={diamond_count} final={final} tools={tools} log={log_path}"
        )
    if "goal_driver_start:collect_resource" not in tools:
        raise AssertionError(f"{run_id}: missing goal_driver_start collect_resource in {tools}")
    for required in (
        "tool_invoke:collect_resource",
        "tool_invoke:craft_item",
        "tool_invoke:smelt_item",
        "tool_invoke:equip_item",
        "goal_driver_result:collect_resource",
    ):
        if required not in tools:
            raise AssertionError(f"{run_id}: missing {required}; tools={tools}")
    if not any(
        event.get("event") == "goal_driver_result"
        and event.get("tool") == "collect_resource"
        and event.get("success") is True
        for event in events
    ):
        raise AssertionError(f"{run_id}: missing successful goal_driver_result in {log_path}")
    return {
        "run_id": run_id,
        "log": str(log_path),
        "diamond_count": diamond_count,
        "tools": tools,
        "arena": arena,
        "event_count": len(events),
    }


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                events.append(json.loads(line))
    return events


def _tool_sequence(events: list[dict[str, object]]) -> list[str]:
    sequence: list[str] = []
    for event in events:
        name = str(event.get("event") or "")
        if name == "goal_driver_start":
            sequence.append(f"goal_driver_start:{event.get('tool')}")
        elif name == "tool_invoke":
            sequence.append(f"tool_invoke:{event.get('tool')}")
        elif name == "tool_continuation":
            sequence.append(f"tool_continuation:{event.get('tool')}")
        elif name == "goal_driver_result":
            sequence.append(f"goal_driver_result:{event.get('tool')}")
    return sequence


def _is_model_event(event: dict[str, object]) -> bool:
    name = str(event.get("event") or "")
    return name.startswith("model_") or name in {
        "agent_start",
        "agent_end",
        "llm_start",
        "llm_end",
        "turn_completed",
        "assistant_message",
        "assistant_final_output",
        "model_tool_call",
        "model_message",
    }


def _grep_secret(path: Path) -> int:
    secret = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not secret:
        return 0
    data = path.read_text(encoding="utf-8", errors="replace")
    return data.count(secret)


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for index in (1, 2):
        reset_world_or_skip()
        log_path = LOG_DIR / f"g2-diamond-ladder-run{index}.jsonl"
        if log_path.exists():
            log_path.unlink()
        result = asyncio.run(run_goal_once(f"g2-diamond-ladder-run{index}", log_path))
        leak_count = _grep_secret(log_path)
        if leak_count:
            raise AssertionError(f"secret token leak detected in {log_path}: {leak_count}")
        result["secret_grep_count"] = leak_count
        results.append(result)
    left = results[0]["tools"]
    right = results[1]["tools"]
    if left != right:
        raise AssertionError(f"G2 tool sequence drifted across resets:\nrun1={left}\nrun2={right}")
    print(json.dumps({"goal": GOAL, "runs": results, "tool_sequence": left}, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
