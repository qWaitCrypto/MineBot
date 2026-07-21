#!/usr/bin/env python3
"""Run the fixed-world AG gate through production interactive ingress.

This is test orchestration only. The child process calls the normal production
entrypoint with a restricted scenario hook; the hook can inject user chat and
scheduled world facts but cannot submit tasks, select tools, or invoke Body
transactions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minebot.app.local_launcher import (  # noqa: E402
    _LOCAL_RUNTIME_DEFAULTS,
    _reset_environment,
    discover_runtime_env_path,
    load_runtime_environment,
    preflight_runtime_environment,
)
from minebot.app.observability import sanitize_observation  # noqa: E402
from minebot.app.real_server_session import (  # noqa: E402
    InteractiveScenarioContext,
    real_server_config_from_env,
    run_real_server_interactive,
)
from minebot.camera.config import discover_camera_config_path  # noqa: E402


MATERIAL_GOAL = (
    "请在不破坏玩家建造的前提下，自主收集木头，制作工作台和木镐，"
    "采集煤炭与铁矿，熔炼铁锭并装备合适工具。遇到不可达目标时使用已有工具探索，"
    "并基于真实世界结果更新计划或报告类型化原因。"
)
GUIDE_NAME = "MineBotGuide"


async def _first_segment(context: InteractiveScenarioContext) -> None:
    await context.wait_for_body_ready(timeout_s=60)
    await context.emit_chat("AGTester", "你好，你是谁？请简短说明你现在能做什么。")
    await asyncio.sleep(12)
    await context.emit_chat("AGTester", f"/goal {MATERIAL_GOAL}")
    await asyncio.sleep(90)
    await context.emit_chat("AGTester", "/pause gate_pause_coverage")
    await asyncio.sleep(20)
    await context.emit_chat("AGTester", "/continue")
    while True:
        await asyncio.sleep(60)


async def _second_segment(context: InteractiveScenarioContext, duration_s: float) -> None:
    started = time.monotonic()

    async def wait_until(offset_s: float) -> bool:
        remaining = offset_s - (time.monotonic() - started)
        if remaining <= 0:
            return True
        await asyncio.sleep(remaining)
        return time.monotonic() - started < duration_s

    try:
        await context.wait_for_body_ready(timeout_s=60)
        if await wait_until(15):
            await context.emit_chat("AGTester", "请回忆刚才的目标和已经确认的世界事实，然后继续当前任务。")
        if await wait_until(75):
            await context.spawn_fake_player_near_bot(GUIDE_NAME, distance=6)
            await context.emit_chat(
                "AGTester",
                f"/goal 请找到并短暂跟随 {GUIDE_NAME}，保持安全距离；完成后如实汇报。",
            )
        if await wait_until(210):
            await context.emit_chat("AGTester", f"/goal {MATERIAL_GOAL}")
        if await wait_until(360):
            await context.set_difficulty("normal")
            await context.spawn_husk_near_bot(distance=2)
        if await wait_until(420):
            await context.clear_hostiles()
            await context.set_difficulty("peaceful")
            await context.emit_chat(
                "AGTester",
                "请暂时不要行动，等待环境中的下一次实质变化后再决定如何继续。",
            )
        if await wait_until(725):
            await context.set_difficulty("normal")
            await context.spawn_husk_near_bot(distance=2)
        if await wait_until(785):
            await context.clear_hostiles()
            await context.set_difficulty("peaceful")
            await context.emit_chat("AGTester", f"/goal {MATERIAL_GOAL}")
        if await wait_until(max(0.0, duration_s - 80)):
            await context.emit_chat("AGTester", "/cancel gate_cancellation_coverage")
        if await wait_until(max(0.0, duration_s - 30)):
            await context.emit_chat("AGTester", "/quit ag_gate_complete")
    finally:
        await context.clear_hostiles()
        await context.set_difficulty("peaceful")
        await context.remove_fake_player(GUIDE_NAME)


def _run_child(
    environment: Mapping[str, str],
    segment: str,
    duration_s: float,
    camera: bool,
    diagnostic_path: str,
) -> None:
    os.environ.clear()
    os.environ.update(environment)
    try:
        config = real_server_config_from_env()
        camera_path = discover_camera_config_path(environ=os.environ) if camera else None
        hook = _first_segment if segment == "first" else lambda context: _second_segment(context, duration_s)
        raise SystemExit(
            asyncio.run(
                run_real_server_interactive(
                    config,
                    None,
                    max_steps=None,
                    camera_config=camera_path,
                    scenario_hook=hook,
                )
            )
        )
    except BaseException as exc:
        if not isinstance(exc, SystemExit) or int(exc.code or 0) != 0:
            payload = sanitize_observation(
                {
                    "segment": segment,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            Path(diagnostic_path).write_text(
                json.dumps(payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        raise


def _trace_summary(path: Path) -> dict[str, object]:
    events: list[dict[str, object]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    counts = Counter(str(event.get("event") or "") for event in events)
    terminal_truths = [
        event.get("terminal_truth")
        for event in events
        if event.get("event") == "session_terminal" and isinstance(event.get("terminal_truth"), dict)
    ]
    world_ids = sorted(
        {
            str(event.get("world_id"))
            for event in events
            if event.get("event") == "runtime_scope" and event.get("world_id")
        }
    )
    secret_matches = len(
        re.findall(r"\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,})\b", path.read_text(encoding="utf-8"))
    ) if path.exists() else 0
    return {
        "trace_records": len(events),
        "event_counts": dict(sorted(counts.items())),
        "model_requests": sum(count for name, count in counts.items() if name in {"llm_start", "model_request"}),
        "transport_errors": counts["body_transport_error"],
        "action_timeouts": counts["body_action_timeout"],
        "progress_yields": counts["progress_yielded"],
        "scenario_failures": counts["scenario_fixture_failed"],
        "world_ids": world_ids,
        "terminal_truths": terminal_truths,
        "governance_events": sum(
            count for name, count in counts.items() if "governance" in name or "mutation" in name
        ),
        "secret_matches": secret_matches,
    }


@dataclass(frozen=True)
class SegmentResult:
    exit_code: int
    elapsed_s: float
    terminated_at_deadline: bool


def _run_segment(
    context: multiprocessing.context.BaseContext,
    environment: Mapping[str, str],
    segment: str,
    duration_s: float,
    *,
    camera: bool,
    hard_stop: bool,
    diagnostic_path: Path,
) -> SegmentResult:
    process = context.Process(
        target=_run_child,
        args=(dict(environment), segment, duration_s, camera, str(diagnostic_path)),
    )
    started = time.monotonic()
    process.start()
    process.join(timeout=duration_s if hard_stop else duration_s + 90)
    terminated_at_deadline = False
    if process.is_alive():
        terminated_at_deadline = hard_stop
        process.terminate()
        process.join(timeout=30)
    return SegmentResult(
        exit_code=process.exitcode or 0,
        elapsed_s=round(time.monotonic() - started, 3),
        terminated_at_deadline=terminated_at_deadline,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local fixed-world AG integration gate.")
    parser.add_argument("--env-file", type=Path, help="Private MineBot runtime profile.")
    parser.add_argument("--run-dir", type=Path, help="Local directory for trace and report.")
    parser.add_argument("--duration-seconds", type=float, default=1800.0)
    parser.add_argument("--restart-after-seconds", type=float, default=600.0)
    parser.add_argument("--no-camera", action="store_true")
    args = parser.parse_args(argv)
    if args.duration_seconds < 1_080 or args.restart_after_seconds < 180:
        parser.error("the AG gate requires at least 1080 seconds and a restart after at least 180 seconds")
    if args.restart_after_seconds > args.duration_seconds - 900:
        parser.error("restart must leave at least 900 seconds for the reconciliation segment")

    env_path = discover_runtime_env_path(args.env_file)
    environment = load_runtime_environment(env_path)
    for key, value in _LOCAL_RUNTIME_DEFAULTS.items():
        environment.setdefault(key, value)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = (args.run_dir or ROOT / "logs" / "agentic-runtime" / f"ag-{stamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    environment["MINEBOT_AGENT_LOG_PATH"] = str(run_dir / "trace.jsonl")
    environment["MINEBOT_AGENT_STATE_DB"] = str(run_dir / "state.sqlite3")
    preflight_runtime_environment(environment, camera=not args.no_camera)

    reset = subprocess.run(
        [str(ROOT / "tools" / "reset-world.sh")],
        cwd=ROOT,
        env=_reset_environment(environment),
        check=False,
    )
    if reset.returncode != 0:
        return reset.returncode

    started = time.time()
    process_context = multiprocessing.get_context("spawn")
    first_diagnostic = run_dir / "first-segment-diagnostic.json"
    first = _run_segment(
        process_context,
        environment,
        "first",
        args.restart_after_seconds,
        camera=not args.no_camera,
        hard_stop=True,
        diagnostic_path=first_diagnostic,
    )
    if not first.terminated_at_deadline:
        report = {
            "started_at": started,
            "duration_seconds": args.duration_seconds,
            "restart_after_seconds": args.restart_after_seconds,
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "first_segment": first.__dict__,
            "classification": "fixture_first_segment_exited_early",
            "diagnostic": _read_diagnostic(first_diagnostic),
            "trace": _trace_summary(run_dir / "trace.jsonl"),
        }
        (run_dir / "gate-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"AG gate fixture failed before restart: {run_dir / 'gate-report.json'}", file=sys.stderr)
        return 1
    second_duration = args.duration_seconds - args.restart_after_seconds
    second_diagnostic = run_dir / "second-segment-diagnostic.json"
    second = _run_segment(
        process_context,
        environment,
        "second",
        second_duration,
        camera=not args.no_camera,
        hard_stop=False,
        diagnostic_path=second_diagnostic,
    )
    report = {
        "started_at": started,
        "duration_seconds": args.duration_seconds,
        "restart_after_seconds": args.restart_after_seconds,
        "first_segment": first.__dict__,
        "second_segment": second.__dict__,
        "first_diagnostic": _read_diagnostic(first_diagnostic),
        "second_diagnostic": _read_diagnostic(second_diagnostic),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "trace": _trace_summary(run_dir / "trace.jsonl"),
    }
    (run_dir / "gate-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"AG gate report: {run_dir / 'gate-report.json'}")
    return 0 if first.exit_code in {-15, 0} and second.exit_code == 0 else 1


def _read_diagnostic(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
