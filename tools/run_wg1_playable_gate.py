#!/usr/bin/env python3
"""WG1 playable-baseline gate for the interactive real-server path."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.config import AppConfigError, agent_language_from_env, provider_registry_from_env  # noqa: E402
from minebot.app.observability import JsonlObservationSink  # noqa: E402
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime, inventory_count  # noqa: E402
from minebot.app.real_server_session import (  # noqa: E402
    _announce_interactive_terminal,
    _goal_driver,
    _interactive_speech_sink,
    _poll_chat_commands,
    safe_evaluate_terminal_truth,
)
from minebot.app.runner import RuntimeTrace  # noqa: E402
from minebot.app.session import AgentSession  # noqa: E402
from minebot.brain.lifecycle import LifecycleState  # noqa: E402
from minebot.contract import Region  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, spawn_or_fail  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs" / "wg1-playable-gate"
SERVER_LOG = ROOT / "test-server" / "logs" / "latest.log"
BOT = "Bot1"
TESTER = "Tester"
ARENA_MIN = (40, 68, 40)
ARENA_MAX = (58, 76, 58)
SPAWN_POS = (44, 70, 44)
REGION = Region("wg1-playable-arena", ARENA_MIN, ARENA_MAX)
COORDINATE_TRIPLE_RE = re.compile(
    r"(?<![\w.-])[\[(]\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*[\])]|\b(?:coordinates?|coords?|position|pos|at)\s*[:=]?\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?|(?:坐标|位置)\s*[:：]?\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?",
    re.IGNORECASE,
)


@dataclass
class GateRun:
    run_id: str
    log_path: Path
    events: list[dict[str, object]]
    server_log_start: int
    results: dict[str, object]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the WG1 interactive playable-baseline gate.")
    parser.add_argument("--runs", type=int, default=2, help="Number of full gate repetitions; WG1 requires 2.")
    parser.add_argument("--timeout-s", type=float, default=90.0, help="Per-stage timeout in seconds.")
    parser.add_argument("--rcon-host", default="127.0.0.1")
    parser.add_argument("--rcon-port", type=int, default=25576)
    parser.add_argument("--rcon-password", default="test")
    parser.add_argument("--rcon-timeout-s", type=float, default=20.0)
    args = parser.parse_args()

    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")
    return asyncio.run(async_main(args))


async def async_main(args: argparse.Namespace) -> int:
    try:
        provider = provider_registry_from_env()
    except AppConfigError as exc:
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}, sort_keys=True), file=sys.stderr)
        return 2

    config = RconConfig(
        host=args.rcon_host,
        port=args.rcon_port,
        password=args.rcon_password,
        timeout_s=args.rcon_timeout_s,
    )
    try:
        rcon = RconClient(config)
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(
            f"SKIP: local RCON unavailable at {config.host}:{config.port}: "
            f"{type(exc).__name__}: {exc}"
        )
        return SKIP_EXIT_CODE

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {"runs": [], "bot": BOT, "arena": {"min": list(ARENA_MIN), "max": list(ARENA_MAX)}}
    try:
        with rcon:
            for index in range(1, args.runs + 1):
                run_id = f"wg1-run{index}-{time.strftime('%Y%m%d-%H%M%S')}"
                log_path = LOG_DIR / f"{run_id}.jsonl"
                if log_path.exists():
                    log_path.unlink()
                gate_run = await run_once(rcon, provider, run_id=run_id, log_path=log_path, timeout_s=args.timeout_s)
                leak_count = grep_secret(log_path)
                if leak_count:
                    raise AssertionError(f"secret token leak detected in {log_path}: {leak_count}")
                gate_run.results["secret_grep_count"] = leak_count
                summary["runs"].append(gate_run.results)
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        summary["error"] = {"type": type(exc).__name__, "message": str(exc)}
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        await provider.aclose()


async def run_once(
    rcon: RconClient,
    provider,
    *,
    run_id: str,
    log_path: Path,
    timeout_s: float,
) -> GateRun:
    server_log_start = _file_size(SERVER_LOG)
    body = ScarpetBody(BOT, rcon)
    prepare_world(rcon, body)
    sink = JsonlObservationSink(log_path)
    speech_sink = _interactive_speech_sink(body)
    language = agent_language_from_env(os.environ, default="Chinese")

    def make_parts(goal_text: str):
        trace = RuntimeTrace(session_id=run_id, sink=sink)
        trace.emit(
            "provider_manifest",
            default_route=provider.default,
            language=language,
            providers=provider.trace_configs(),
        )
        return build_phase1_agent_runtime(
            body=body,
            goal_text=goal_text,
            model_provider=provider,
            config=Phase1RuntimeConfig(
                natural_region=REGION,
                recovery_respawn_pos=SPAWN_POS,
                recovery_gamemode="survival",
                speech_sink=speech_sink,
            ),
            agent_name=f"MineBotWG1-{run_id}",
            language=language,
            trace=trace,
        )

    session = AgentSession(make_parts, goal_driver=_goal_driver)
    results: dict[str, object] = {"run_id": run_id, "log": str(log_path)}
    try:
        await stage_hello(rcon, body, session, log_path, server_log_start=server_log_start, timeout_s=timeout_s)
        await stage_collect(rcon, body, session, log_path, timeout_s=timeout_s)
        await stage_attack(rcon, body, session, log_path, timeout_s=timeout_s)
        await stage_pause_continue_quit(rcon, body, session, log_path, timeout_s=timeout_s)
        events = read_jsonl(log_path)
        results.update(
            {
                "event_count": len(events),
                "model_event_count": sum(1 for event in events if is_model_event(event)),
                "oak_log_count": inventory_count(body, "oak_log"),
                "husk_count": count_entity(rcon, "husk"),
                "tool_sequence": tool_sequence(events),
                "server_chat_lines": bot_chat_lines_since(server_log_start),
            }
        )
        return GateRun(run_id, log_path, events, server_log_start, results)
    finally:
        if session.parts is not None:
            session.parts.runtime.trace.emit(
                "wg1_gate_terminal",
                run_id=run_id,
                observability=body.observability_snapshot(max_events=64, max_traces=32, max_requests=32),
            )
            session.parts.runtime.trace.close()
        cleanup_subjects(rcon)


async def stage_hello(
    rcon: RconClient,
    body: ScarpetBody,
    session: AgentSession,
    log_path: Path,
    *,
    server_log_start: int,
    timeout_s: float,
) -> None:
    before = len(read_jsonl(log_path))
    inject_chat(rcon, "hello")

    def done() -> bool:
        events = read_jsonl(log_path)[before:]
        return (
            any(event.get("event") == "chat_message" and event.get("content") == "hello" for event in events)
            and any(event.get("event") == "assistant_final_output" for event in events)
            and bool(bot_chat_lines_since(server_log_start))
        )

    await drive_until(session, body, log_path, done, timeout_s=timeout_s)
    events = read_jsonl(log_path)[before:]
    require(any(event.get("event") == "chat_message" for event in events), "hello did not enter chat_message trace")
    require(any(event.get("event") == "assistant_final_output" for event in events), "hello produced no assistant reply")
    chat_lines = bot_chat_lines_since(server_log_start)
    require(chat_lines, "hello assistant reply did not appear in server chat")
    require(not any(COORDINATE_TRIPLE_RE.search(line) for line in chat_lines), "outbound chat leaked raw coordinates")
    trace(session, "wg1_stage_result", stage="hello", passed=True)


async def stage_collect(
    rcon: RconClient,
    body: ScarpetBody,
    session: AgentSession,
    log_path: Path,
    *,
    timeout_s: float,
) -> None:
    before = len(read_jsonl(log_path))
    inject_chat(rcon, "collect 3 oak_log")

    def done() -> bool:
        events = read_jsonl(log_path)[before:]
        return (
            any(event.get("event") == "chat_goal_promoted" for event in events)
            and any(event.get("event") == "goal_driver_result" and event.get("tool") == "collect_resource" for event in events)
            and inventory_count(body, "oak_log") >= 3
        )

    final = await drive_until(session, body, log_path, done, timeout_s=timeout_s)
    truth = safe_evaluate_terminal_truth(body, "collect 3 oak_log", final, session=session)
    if truth.satisfied:
        completed = session.complete_current_goal("terminal_truth_satisfied")
        truth = safe_evaluate_terminal_truth(body, "collect 3 oak_log", completed, session=session)
        _announce_interactive_terminal(body, truth)

    events = read_jsonl(log_path)[before:]
    require(any(event.get("event") == "chat_goal_promoted" for event in events), "collect chat was not promoted")
    require(any(event.get("event") == "goal_driver_start" and event.get("tool") == "collect_resource" for event in events), "collect did not start goal_driver")
    require(not any(is_model_event(event) for event in events), "collect promoted segment used a model event")
    require(truth.satisfied and int(truth.inventory_count or 0) >= 3, f"collect terminal truth not satisfied: {truth}")
    trace(session, "wg1_stage_result", stage="collect", passed=True, terminal_truth=truth.to_trace())


async def stage_attack(
    rcon: RconClient,
    body: ScarpetBody,
    session: AgentSession,
    log_path: Path,
    *,
    timeout_s: float,
) -> None:
    spawn_husk_near_bot(rcon, body)
    before = len(read_jsonl(log_path))
    inject_chat(rcon, "attack the husk")

    def done() -> bool:
        events = read_jsonl(log_path)[before:]
        return (
            any(event.get("event") == "model_tool_call" and event.get("tool") == "engage_entity" for event in events)
            and any(
                event.get("event") == "tool_result"
                and event.get("tool") == "engage_entity"
                and event.get("reason") == "killed"
                for event in events
            )
        )

    await drive_until(session, body, log_path, done, timeout_s=timeout_s)
    events = read_jsonl(log_path)[before:]
    require(any(event.get("event") == "model_tool_call" and event.get("tool") == "engage_entity" for event in events), "attack did not call engage_entity via model")
    require(any(event.get("event") == "tool_result" and event.get("tool") == "engage_entity" and event.get("reason") == "killed" for event in events), "engage_entity did not report killed")
    require(any(event.name == "engageDone" and event.data.get("reason") == "killed" for event in body.event_log), "Body event log has no engageDone/killed")
    trace(session, "wg1_stage_result", stage="attack", passed=True, husk_count=count_entity(rcon, "husk"))


async def stage_pause_continue_quit(
    rcon: RconClient,
    body: ScarpetBody,
    session: AgentSession,
    log_path: Path,
    *,
    timeout_s: float,
) -> None:
    before = len(read_jsonl(log_path))
    inject_chat(rcon, "/pause")
    paused = await drive_until(
        session,
        body,
        log_path,
        lambda: any(event.get("event") == "user_message" and event.get("command") == "pause" for event in read_jsonl(log_path)[before:]),
        timeout_s=timeout_s,
    )
    require(paused.lifecycle in {LifecycleState.INTERRUPTED, LifecycleState.YIELDED, LifecycleState.IDLE, LifecycleState.ACTIVE}, f"unexpected pause lifecycle {paused.lifecycle}")

    cont_before = len(read_jsonl(log_path))
    inject_chat(rcon, "/continue")
    resumed = await drive_until(
        session,
        body,
        log_path,
        lambda: any(event.get("event") == "user_message" and event.get("command") == "continue" for event in read_jsonl(log_path)[cont_before:]),
        timeout_s=timeout_s,
    )
    require(resumed.status != "failed", f"continue failed: {resumed}")

    quit_before = len(read_jsonl(log_path))
    inject_chat(rcon, "/quit")
    final = await drive_until(
        session,
        body,
        log_path,
        lambda: any(event.get("event") == "user_message" and event.get("command") == "quit" for event in read_jsonl(log_path)[quit_before:]),
        timeout_s=timeout_s,
    )
    require(final.status == "quit", f"quit did not return status=quit: {final}")
    trace(session, "wg1_stage_result", stage="pause_continue_quit", passed=True, final_status=final.status)


async def drive_until(
    session: AgentSession,
    body: ScarpetBody,
    log_path: Path,
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
) -> object:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        _poll_chat_commands(session, body)
        last = await session.step()
        if predicate():
            return last
        settle_deadline = time.monotonic() + 1.0
        while time.monotonic() < settle_deadline:
            if predicate():
                return last
            await asyncio.sleep(0.1)
        if last.status == "quit":
            return last
        await asyncio.sleep(0.1 if last.lifecycle.value == "idle" else 0)
    raise TimeoutError(f"WG1 stage timed out after {timeout_s}s; last={last}; log={log_path}")


def prepare_world(rcon: RconClient, body: ScarpetBody) -> None:
    for command in (
        "script load minebot global",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "gamerule randomTickSpeed 0",
        "time set day",
        "weather clear",
        "difficulty normal",
        f"player {BOT} kill",
        "kill @e[type=husk]",
        "kill @e[type=item]",
        f"fill {ARENA_MIN[0]} {ARENA_MIN[1]} {ARENA_MIN[2]} {ARENA_MAX[0]} {ARENA_MAX[1]} {ARENA_MAX[2]} air",
        f"fill {ARENA_MIN[0]} 69 {ARENA_MIN[2]} {ARENA_MAX[0]} 69 {ARENA_MAX[2]} smooth_stone",
        "script in minebot run minebot_reset()",
    ):
        command_rcon(rcon, command)
    for pos in ((48, 70, 44), (49, 70, 44), (50, 70, 44), (51, 70, 44)):
        command_rcon(rcon, f"setblock {pos[0]} {pos[1]} {pos[2]} oak_log", delay=0.0)
    spawn_or_fail(body, SPAWN_POS)
    command_rcon(rcon, f"tp {BOT} {SPAWN_POS[0]} {SPAWN_POS[1]} {SPAWN_POS[2]} -90 0")
    command_rcon(rcon, f"gamemode survival {BOT}")
    command_rcon(rcon, f"effect clear {BOT}")
    command_rcon(rcon, f"clear {BOT}")
    for slot in range(46):
        body.transport.request(f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
    state = body.get_state()
    if state.missing:
        raise AssertionError(f"{BOT} is missing after spawn")


def cleanup_subjects(rcon: RconClient) -> None:
    for command in (f"player {BOT} kill", "kill @e[type=husk]", "kill @e[type=item]"):
        try:
            command_rcon(rcon, command, delay=0.0)
        except Exception:
            pass


def spawn_husk_near_bot(rcon: RconClient, body: ScarpetBody) -> None:
    state = body.get_state()
    x = round(state.pos[0]) + 2
    y = round(state.pos[1])
    z = round(state.pos[2])
    command_rcon(rcon, "kill @e[type=husk]", delay=0.05)
    command_rcon(rcon, f"summon husk {x} {y} {z} {{NoAI:1b,PersistenceRequired:1b,Health:1f}}", delay=0.2)
    require(count_entity(rcon, "husk") >= 1, "failed to spawn husk")


def inject_chat(rcon: RconClient, message: str) -> None:
    command_rcon(rcon, f"script in minebot run emit_agent_chat('{BOT}', '{TESTER}', '{escape_scarpet_string(message)}')", delay=0.05)


def command_rcon(rcon: RconClient, command: str, delay: float = 0.05) -> str:
    out = rcon.command(command)
    if delay:
        time.sleep(delay)
    return out


def count_entity(rcon: RconClient, entity_type: str) -> int:
    out = command_rcon(rcon, f"script run length(entity_selector('@e[type={entity_type}]'))", delay=0.0)
    try:
        return int(out.split("=")[-1].split("(")[0].strip())
    except Exception:
        return -1


def trace(session: AgentSession, event: str, **fields: object) -> None:
    if session.parts is not None:
        session.parts.runtime.trace.emit(event, **fields)


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                events.append(json.loads(line))
    return events


def is_model_event(event: dict[str, object]) -> bool:
    name = str(event.get("event") or "")
    return name.startswith("model_") or name in {
        "agent_start",
        "agent_end",
        "llm_start",
        "llm_end",
        "assistant_message",
        "assistant_final_output",
        "model_tool_call",
        "model_message",
    }


def tool_sequence(events: list[dict[str, object]]) -> list[str]:
    sequence: list[str] = []
    for event in events:
        name = str(event.get("event") or "")
        if name in {"goal_driver_start", "tool_invoke", "tool_result", "composition_tool_result", "goal_driver_result"}:
            tool = event.get("tool")
            reason = event.get("reason")
            sequence.append(f"{name}:{tool}:{reason}" if reason else f"{name}:{tool}")
    return sequence


def bot_chat_lines_since(offset: int) -> list[str]:
    if not SERVER_LOG.exists():
        return []
    size = SERVER_LOG.stat().st_size
    if offset > size:
        offset = 0
    with SERVER_LOG.open("rb") as fh:
        fh.seek(offset)
        text = fh.read().decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if f"[{BOT}]" in line]


def grep_secret(path: Path) -> int:
    if not path.exists():
        return 0
    data = path.read_text(encoding="utf-8", errors="replace")
    count = 0
    for name in ("ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "MINEBOT_LLM_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            count += data.count(secret)
    return count


def escape_scarpet_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
