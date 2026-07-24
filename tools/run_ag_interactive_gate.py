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
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minebot.app.local_launcher import (  # noqa: E402
    _LOCAL_RUNTIME_DEFAULTS,
    _reset_environment,
    discover_runtime_env_path,
    load_runtime_environment,
    preflight_runtime_environment,
)
from minebot.app.autonomy_quality import AG_FP30_YARDSTICK, evaluate_autonomy_quality  # noqa: E402
from minebot.app.observability import sanitize_observation  # noqa: E402
from minebot.app.real_server_session import (  # noqa: E402
    AG_FP30_GOAL,
    InteractiveScenarioContext,
    real_server_config_from_env,
    run_real_server_interactive,
)
from minebot.camera.config import discover_camera_config_path  # noqa: E402


# Keep the scenario ingress byte-for-byte aligned with the frozen evaluator.
MATERIAL_GOAL = AG_FP30_GOAL
GUIDE_NAME = "MineBotGuide"
IDLE_PROMPT = "请暂时不要行动，等待环境中的下一次实质变化后再决定如何继续。"
_BODY_READY_TIMEOUT_S = 120.0
_SECOND_SEGMENT_EXIT_GRACE_S = 90.0
_CAMERA_RUNNING_PHASES = frozenset({"starting", "connecting", "ready", "stopping"})
_MIN_AG_DURATION_S = 1_800.0
_MAX_AG_DURATION_S = 3_600.0
_MIN_SECOND_SEGMENT_S = 1_020.0
_IDLE_PROMPT_OFFSET_S = 420.0
_IDLE_WAKE_OFFSET_S = 870.0
_IDLE_CLEAR_OFFSET_S = 930.0
_MATERIAL_RESUME_OFFSET_S = 935.0


async def _first_segment(
    context: InteractiveScenarioContext,
    mark_ready: Callable[[], None],
) -> None:
    await context.wait_for_body_ready(timeout_s=60)
    mark_ready()
    await context.emit_chat("AGTester", "你好，你是谁？请简短说明你现在能做什么。")
    await asyncio.sleep(12)
    await context.emit_chat("AGTester", f"/goal {MATERIAL_GOAL}")
    await asyncio.sleep(90)
    await context.emit_chat("AGTester", "/pause gate_pause_coverage")
    await asyncio.sleep(20)
    await context.emit_chat("AGTester", "/continue")
    while True:
        await asyncio.sleep(60)


async def _quality_segment(
    context: InteractiveScenarioContext,
    duration_s: float,
    mark_ready: Callable[[], None],
) -> None:
    await context.wait_for_body_ready(timeout_s=60)
    mark_ready()
    started = time.monotonic()
    await context.emit_chat("AGTester", f"/goal {MATERIAL_GOAL}")
    await asyncio.sleep(max(0.0, duration_s - (time.monotonic() - started)))
    await context.emit_chat("AGTester", "/quit ag_quality_gate_complete")


async def _second_segment(
    context: InteractiveScenarioContext,
    duration_s: float,
    mark_ready: Callable[[], None],
) -> None:
    async def wait_until(offset_s: float) -> bool:
        remaining = offset_s - (time.monotonic() - started)
        if remaining <= 0:
            return True
        await asyncio.sleep(remaining)
        return time.monotonic() - started < duration_s

    try:
        await context.wait_for_body_ready(timeout_s=60)
        mark_ready()
        started = time.monotonic()
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
        if await wait_until(_IDLE_PROMPT_OFFSET_S):
            await context.clear_hostiles()
            await context.set_difficulty("peaceful")
            idle_marker = await context.emit_chat(
                "AGTester",
                IDLE_PROMPT,
            )
            await context.wait_for_idle_quiescence(after_trace_seq=idle_marker)
        if await wait_until(_IDLE_WAKE_OFFSET_S):
            await context.set_difficulty("normal")
            await context.provoke_husk_attack()
        if await wait_until(_IDLE_CLEAR_OFFSET_S):
            await context.clear_hostiles()
            await context.set_difficulty("peaceful")
        if await wait_until(_MATERIAL_RESUME_OFFSET_S):
            await context.emit_chat("AGTester", f"/goal {MATERIAL_GOAL}")
        if await wait_until(max(0.0, duration_s - 80)):
            await context.emit_chat("AGTester", "/cancel gate_cancellation_coverage")
        await asyncio.sleep(max(0.0, duration_s - (time.monotonic() - started)))
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
    ready_event: Any,
) -> None:
    os.environ.clear()
    os.environ.update(environment)
    try:
        config = real_server_config_from_env()
        camera_path = discover_camera_config_path(environ=os.environ) if camera else None
        if segment == "quality":
            hook = lambda context: _quality_segment(context, duration_s, ready_event.set)
        elif segment == "first":
            hook = lambda context: _first_segment(context, ready_event.set)
        else:
            hook = lambda context: _second_segment(context, duration_s, ready_event.set)
        raise SystemExit(
            asyncio.run(
                run_real_server_interactive(
                    config,
                    None,
                    max_steps=None,
                    camera_config=camera_path,
                    scenario_hook=hook,
                    terminal_goal=AG_FP30_GOAL,
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


def _trace_summary(path: Path, *, active_elapsed_s: float | None = None) -> dict[str, object]:
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
    canonical_terminal_truths = [
        truth
        for truth in terminal_truths
        if isinstance(truth, dict)
        and " ".join(str(truth.get("goal") or "").split()).casefold()
        == " ".join(AG_FP30_GOAL.split()).casefold()
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
    ready_to_terminal_elapsed_s = _trace_elapsed_s(events, "scenario_body_ready", "session_terminal")
    idle_window = _idle_window_summary(events)
    quality = evaluate_autonomy_quality(
        events,
        yardstick=AG_FP30_YARDSTICK,
        active_window_s=active_elapsed_s,
    )
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
        "canonical_terminal_truths": canonical_terminal_truths,
        "authoritative_satisfied": bool(
            canonical_terminal_truths
            and canonical_terminal_truths[-1].get("satisfied") is True
            and isinstance(canonical_terminal_truths[-1].get("facts"), dict)
            and canonical_terminal_truths[-1]["facts"].get("terminal_satisfied") is True
        ),
        "autonomy_quality": quality,
        "ready_to_terminal_elapsed_s": ready_to_terminal_elapsed_s,
        "idle_window": idle_window,
        "governance_events": sum(
            count for name, count in counts.items() if "governance" in name or "mutation" in name
        ),
        "secret_matches": secret_matches,
    }


def _quality_gate_passes(
    segment: "SegmentResult",
    quality: object,
    *,
    active_duration_met: bool,
) -> bool:
    """Keep the configured active-window duration part of the gate.

    The child can exit cleanly before the deadline (for example after a
    provider/runtime failure).  A clean exit and a passing short trace are not
    evidence for a 30-60 minute gate, so the duration check must be an explicit
    exit-code input rather than report-only metadata.
    """

    return bool(
        segment.body_ready
        and segment.exit_code == 0
        and active_duration_met
        and isinstance(quality, dict)
        and quality.get("verdict") == "pass"
    )


def _trace_elapsed_s(
    events: list[dict[str, object]],
    start_event: str,
    end_event: str,
) -> float | None:
    starts = [event.get("ts") for event in events if event.get("event") == start_event]
    ends = [event.get("ts") for event in events if event.get("event") == end_event]
    if not starts or not ends:
        return None
    start = next((value for value in starts if isinstance(value, (int, float))), None)
    end = next((value for value in reversed(ends) if isinstance(value, (int, float))), None)
    if start is None or end is None:
        return None
    return round(float(end) - float(start), 3)


def _idle_window_summary(events: list[dict[str, object]]) -> dict[str, object] | None:
    idle_prompt = next(
        (
            event
            for event in events
            if event.get("event") == "scenario_chat_emitted" and event.get("message") == IDLE_PROMPT
        ),
        None,
    )
    if idle_prompt is None or not isinstance(idle_prompt.get("ts"), (int, float)):
        return None
    prompt_at = float(idle_prompt["ts"])
    quiescent = next(
        (
            event
            for event in events
            if event.get("event") == "autonomy_decision"
            and event.get("action") == "park"
            and event.get("reason") == "checkpoint_wait_event"
            and isinstance(event.get("ts"), (int, float))
            and float(event["ts"]) > prompt_at
        ),
        None,
    )
    if quiescent is None or not isinstance(quiescent.get("ts"), (int, float)):
        return {"prompt_at": prompt_at, "quiescent_at": None}
    start = float(quiescent["ts"])
    scenario_trigger = next(
        (
            event
            for event in events
            if event.get("event") == "scenario_husk_attack_observed"
            and isinstance(event.get("ts"), (int, float))
            and float(event["ts"]) > start
        ),
        None,
    )
    material_event = next(
        (
            event
            for event in events
            if event.get("event") == "body_event_wake"
            and isinstance(event.get("ts"), (int, float))
            and float(event["ts"]) > start
        ),
        None,
    )
    trigger_at = (
        None
        if scenario_trigger is None or not isinstance(scenario_trigger.get("ts"), (int, float))
        else float(scenario_trigger["ts"])
    )
    event_at = (
        None
        if material_event is None or not isinstance(material_event.get("ts"), (int, float))
        else float(material_event["ts"])
    )
    window_end = event_at if event_at is not None else trigger_at
    if window_end is None:
        return {
            "prompt_at": prompt_at,
            "quiescent_at": start,
            "scenario_triggered_at": None,
            "material_event_at": None,
        }
    clear = next(
        (
            event
            for event in events
            if event.get("event") == "scenario_hostiles_cleared"
            and isinstance(event.get("ts"), (int, float))
            and float(event["ts"]) > (trigger_at if trigger_at is not None else window_end)
        ),
        None,
    )
    clear_at = float(clear["ts"]) if clear is not None and isinstance(clear.get("ts"), (int, float)) else None
    upper_bound = clear_at if clear_at is not None else window_end + 60.0
    idle_model_requests = sum(
        1
        for event in events
        if event.get("event") == "llm_start"
        and isinstance(event.get("ts"), (int, float))
        and start <= float(event["ts"]) < window_end
    )
    wake_model_requests = sum(
        1
        for event in events
        if event.get("event") == "llm_start"
        and isinstance(event.get("ts"), (int, float))
        and event_at is not None
        and event_at <= float(event["ts"]) < upper_bound
    )
    return {
        "prompt_at": prompt_at,
        "quiescent_at": start,
        "scenario_triggered_at": trigger_at,
        "material_event_at": event_at,
        "cleared_at": clear_at,
        "duration_s": round(window_end - start, 3),
        "model_requests_during_idle": idle_model_requests,
        "wake_event_name": None if material_event is None else material_event.get("name"),
        "model_requests_after_material_event": wake_model_requests,
    }


def _camera_summary(state: Mapping[str, object] | None) -> dict[str, object] | None:
    if state is None:
        return None
    return {
        key: state.get(key)
        for key in ("phase", "pid", "target", "recording", "live", "error")
        if key in state
    }


def _require_stopped_camera(camera_path: Path | None) -> dict[str, object] | None:
    if camera_path is None:
        return None
    from minebot.camera.service import service_status

    state = service_status(camera_path)
    if state.get("phase") in _CAMERA_RUNNING_PHASES:
        raise RuntimeError("AG gate requires the configured Camera service to be stopped before it starts")
    return _camera_summary(state)


def _stop_gate_camera(camera_path: Path | None) -> dict[str, object] | None:
    if camera_path is None:
        return None
    from minebot.camera.service import stop_service

    return _camera_summary(stop_service(camera_path))


@dataclass(frozen=True)
class SegmentResult:
    exit_code: int
    elapsed_s: float
    active_elapsed_s: float
    ready_elapsed_s: float | None
    body_ready: bool
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
    ready_event = context.Event()
    process = context.Process(
        target=_run_child,
        args=(dict(environment), segment, duration_s, camera, str(diagnostic_path), ready_event),
    )
    started = time.monotonic()
    process.start()
    if not ready_event.wait(timeout=_BODY_READY_TIMEOUT_S):
        if process.is_alive():
            process.terminate()
            process.join(timeout=30)
        return SegmentResult(
            exit_code=process.exitcode or 0,
            elapsed_s=round(time.monotonic() - started, 3),
            active_elapsed_s=0.0,
            ready_elapsed_s=None,
            body_ready=False,
            terminated_at_deadline=False,
        )

    ready_at = time.monotonic()
    process.join(timeout=duration_s if hard_stop else duration_s + _SECOND_SEGMENT_EXIT_GRACE_S)
    terminated_at_deadline = False
    if process.is_alive():
        terminated_at_deadline = hard_stop
        process.terminate()
        process.join(timeout=30)
    return SegmentResult(
        exit_code=process.exitcode or 0,
        elapsed_s=round(time.monotonic() - started, 3),
        active_elapsed_s=round(time.monotonic() - ready_at, 3),
        ready_elapsed_s=round(ready_at - started, 3),
        body_ready=True,
        terminated_at_deadline=terminated_at_deadline,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local fixed-world AG integration gate.")
    parser.add_argument("--env-file", type=Path, help="Private MineBot runtime profile.")
    parser.add_argument("--run-dir", type=Path, help="Local directory for trace and report.")
    parser.add_argument("--duration-seconds", type=float, default=1800.0)
    parser.add_argument("--restart-after-seconds", type=float, default=600.0)
    parser.add_argument(
        "--runtime-matrix",
        action="store_true",
        help="Run the legacy restart/idle/follow/combat/cancel regression scenario instead of the scored quality window.",
    )
    parser.add_argument("--no-camera", action="store_true")
    args = parser.parse_args(argv)
    if args.duration_seconds < _MIN_AG_DURATION_S or args.duration_seconds > _MAX_AG_DURATION_S:
        parser.error("the AG quality gate requires 1800-3600 seconds")
    if args.runtime_matrix and args.restart_after_seconds < 180:
        parser.error("the runtime matrix requires a restart after at least 180 seconds")
    if args.runtime_matrix and args.restart_after_seconds > args.duration_seconds - _MIN_SECOND_SEGMENT_S:
        parser.error("restart must leave at least 1020 seconds for the reconciliation segment")

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
    camera_path = discover_camera_config_path(environ=environment) if not args.no_camera else None
    camera_initial = _require_stopped_camera(camera_path)

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
    if not args.runtime_matrix:
        diagnostic = run_dir / "quality-segment-diagnostic.json"
        quality_segment = _run_segment(
            process_context,
            environment,
            "quality",
            args.duration_seconds,
            camera=not args.no_camera,
            hard_stop=False,
            diagnostic_path=diagnostic,
        )
        final_camera_cleanup = _stop_gate_camera(camera_path)
        report = {
            "started_at": started,
            "mode": "autonomy_quality",
            "duration_seconds": args.duration_seconds,
            "segment": quality_segment.__dict__,
            "diagnostic": _read_diagnostic(diagnostic),
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "coverage": {
                "active_elapsed_s": quality_segment.active_elapsed_s,
                "configured_duration_s": args.duration_seconds,
                "active_duration_met": quality_segment.active_elapsed_s >= args.duration_seconds,
            },
            "trace": _trace_summary(run_dir / "trace.jsonl", active_elapsed_s=quality_segment.active_elapsed_s),
            "camera": {
                "initial": camera_initial,
                "after_gate": final_camera_cleanup,
            },
        }
        (run_dir / "gate-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"AG quality gate report: {run_dir / 'gate-report.json'}")
        quality = report["trace"].get("autonomy_quality")
        return 0 if _quality_gate_passes(
            quality_segment,
            quality,
            active_duration_met=bool(report["coverage"].get("active_duration_met")),
        ) else 1

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
    first_camera_cleanup = _stop_gate_camera(camera_path)
    if not first.body_ready or not first.terminated_at_deadline:
        report = {
            "started_at": started,
            "duration_seconds": args.duration_seconds,
            "restart_after_seconds": args.restart_after_seconds,
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "first_segment": first.__dict__,
            "classification": (
                "fixture_first_segment_not_ready"
                if not first.body_ready
                else "fixture_first_segment_exited_early"
            ),
            "diagnostic": _read_diagnostic(first_diagnostic),
            "trace": _trace_summary(run_dir / "trace.jsonl", active_elapsed_s=first.active_elapsed_s),
            "camera": {
                "initial": camera_initial,
                "after_first_segment": first_camera_cleanup,
            },
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
    final_camera_cleanup = _stop_gate_camera(camera_path)
    active_elapsed_s = round(first.active_elapsed_s + second.active_elapsed_s, 3)
    report = {
        "started_at": started,
        "mode": "runtime_matrix",
        "duration_seconds": args.duration_seconds,
        "restart_after_seconds": args.restart_after_seconds,
        "first_segment": first.__dict__,
        "second_segment": second.__dict__,
        "first_diagnostic": _read_diagnostic(first_diagnostic),
        "second_diagnostic": _read_diagnostic(second_diagnostic),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "coverage": {
            "active_elapsed_s": active_elapsed_s,
            "configured_duration_s": args.duration_seconds,
            "active_duration_met": active_elapsed_s >= args.duration_seconds,
        },
        "trace": _trace_summary(run_dir / "trace.jsonl", active_elapsed_s=active_elapsed_s),
        "camera": {
            "initial": camera_initial,
            "after_first_segment": first_camera_cleanup,
            "after_gate": final_camera_cleanup,
        },
    }
    (run_dir / "gate-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"AG gate report: {run_dir / 'gate-report.json'}")
    terminal_ok = bool(report["trace"].get("authoritative_satisfied"))
    return 0 if first.body_ready and second.body_ready and first.exit_code in {-15, 0} and second.exit_code == 0 and terminal_ok else 1


def _read_diagnostic(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
