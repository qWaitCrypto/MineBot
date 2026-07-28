#!/usr/bin/env python3
"""Run the fixed-world AG gate through production interactive ingress.

The production child is Java-only and receives no RCON configuration. A
separate parent-owned fixture uses RCON for world setup and public FakePlayer
chat, keeping test orchestration out of the Agent process and its trace.
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
from typing import Awaitable, Callable, Mapping

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
    real_server_config_from_env,
    run_real_server_interactive,
)
from minebot.camera.config import discover_camera_config_path  # noqa: E402
from minebot.game import RconClient  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402


# Keep the scenario ingress byte-for-byte aligned with the frozen evaluator.
MATERIAL_GOAL = AG_FP30_GOAL
GUIDE_NAME = "MineBotGuide"
FIXTURE_SENDER = "AGT"
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
_FIXTURE_CHAT_DISTANCE = 256
_LOCAL_JAVA_BODY_URL = "ws://127.0.0.1:8767"
_MAX_MINECRAFT_COMMAND_LENGTH = 256
_RCON_ENV_KEYS = frozenset(
    {
        "MINEBOT_REAL_RCON_HOST",
        "MINEBOT_REAL_RCON_PORT",
        "MINEBOT_REAL_RCON_PASSWORD",
        "MINEBOT_REAL_RCON_TIMEOUT",
    }
)


@dataclass(frozen=True)
class _FixtureBotState:
    missing: bool
    pos: tuple[float, float, float]
    health: float


class ExternalInteractiveScenarioContext:
    """Restricted external fixture surface for a production Java session."""

    def __init__(
        self,
        *,
        bot_name: str,
        chat_sender: str,
        rcon: RconClient,
        production_trace_path: Path,
        fixture_trace_path: Path,
    ) -> None:
        self.bot_name = bot_name
        self.chat_sender = chat_sender
        self._rcon = rcon
        self._production_trace_path = production_trace_path
        self._fixture_trace_path = fixture_trace_path
        self._fixture_seq = 0
        self._chat_sender_ready = False

    async def emit_chat(self, sender: str, message: str) -> int:
        if sender != self.chat_sender:
            raise ValueError(
                f"external gate chat sender must be {self.chat_sender!r}, got {sender!r}"
            )
        text = str(message).strip()
        if not text or "\n" in text or "\r" in text:
            raise ValueError("scenario chat message must be one non-empty line")
        if not self._chat_sender_ready:
            await self._spawn_chat_sender()
        baseline_seq = max(
            (int(record.get("seq") or 0) for record in self._production_records()),
            default=0,
        )
        command = f"execute as {sender} run me {text}"
        if len(command) > _MAX_MINECRAFT_COMMAND_LENGTH:
            raise ValueError(
                "public fixture chat command exceeds Minecraft's 256-character limit"
            )
        await asyncio.to_thread(self._rcon.request, command)
        self._emit_fixture_event(
            "scenario_chat_emitted",
            sender=sender,
            message=text,
            production_trace_seq=baseline_seq,
        )
        return baseline_seq

    async def wait_for_idle_quiescence(
        self,
        *,
        after_trace_seq: int,
        timeout_s: float = 120.0,
    ) -> None:
        if after_trace_seq < 0:
            raise ValueError("idle marker sequence must not be negative")
        record = await self._wait_for_production_record(
            lambda candidate: (
                int(candidate.get("seq") or 0) > after_trace_seq
                and candidate.get("event") == "autonomy_decision"
                and candidate.get("action") == "park"
                and candidate.get("reason") == "checkpoint_wait_event"
            ),
            timeout_s=timeout_s,
        )
        self._emit_fixture_event(
            "scenario_idle_quiescent",
            after_trace_seq=after_trace_seq,
            production_trace_seq=int(record.get("seq") or 0),
        )

    async def wait_for_body_ready(self, *, timeout_s: float = 60.0) -> None:
        if timeout_s <= 0:
            raise ValueError("fixture ready timeout must be positive")
        deadline = time.monotonic() + timeout_s
        while True:
            state = await self._read_bot_state()
            if not state.missing:
                self._emit_fixture_event(
                    "scenario_body_ready",
                    position=list(state.pos),
                )
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("fixture Bot did not become ready before timeout")
            await asyncio.sleep(0.25)

    async def spawn_fake_player(
        self,
        name: str,
        position: tuple[int, int, int],
    ) -> None:
        x, y, z = (int(value) for value in position)
        await asyncio.to_thread(self._rcon.request, f"player {name} kill")
        await asyncio.to_thread(
            self._rcon.request,
            f"player {name} spawn at {x} {y} {z}",
        )
        self._emit_fixture_event(
            "scenario_fake_player_spawned",
            name=name,
            position=[x, y, z],
        )

    async def remove_fake_player(self, name: str) -> None:
        await asyncio.to_thread(self._rcon.request, f"player {name} kill")
        self._emit_fixture_event("scenario_fake_player_removed", name=name)

    async def spawn_fake_player_near_bot(self, name: str, *, distance: int = 5) -> None:
        if distance < 1 or distance > 32:
            raise ValueError("fixture fake-player distance must be between 1 and 32")
        state = await self._require_bot_state()
        await self.spawn_fake_player(
            name,
            (round(state.pos[0]) + distance, round(state.pos[1]), round(state.pos[2])),
        )

    async def set_difficulty(self, difficulty: str) -> None:
        normalized = str(difficulty).lower()
        if normalized not in {"peaceful", "easy", "normal", "hard"}:
            raise ValueError(f"unsupported fixture difficulty: {difficulty!r}")
        await asyncio.to_thread(self._rcon.request, f"difficulty {normalized}")
        self._emit_fixture_event("scenario_difficulty_set", difficulty=normalized)

    async def spawn_husk_near_bot(
        self,
        *,
        distance: int = 2,
        offset: tuple[int, int] | None = None,
    ) -> None:
        if distance < 1 or distance > 8:
            raise ValueError("fixture hostile distance must be between 1 and 8")
        state = await self._require_bot_state()
        dx, dz = (distance, 0) if offset is None else offset
        if (dx == 0 and dz == 0) or abs(dx) > 8 or abs(dz) > 8:
            raise ValueError("fixture hostile offset must be non-zero and within 8 blocks")
        x = round(state.pos[0]) + dx
        y = round(state.pos[1])
        z = round(state.pos[2]) + dz
        await asyncio.to_thread(
            self._rcon.request,
            "kill @e[type=minecraft:husk]",
        )
        await asyncio.to_thread(
            self._rcon.request,
            f"summon husk {x} {y} {z} {{PersistenceRequired:1b}}",
        )
        self._emit_fixture_event(
            "scenario_husk_spawned",
            position=[x, y, z],
            distance=distance,
        )

    async def provoke_husk_attack(self, *, timeout_s: float = 45.0) -> None:
        if timeout_s <= 0:
            raise ValueError("fixture attack timeout must be positive")
        baseline = await self._require_bot_state()
        deadline = time.monotonic() + timeout_s
        offsets = ((1, 0), (-1, 0), (0, 1), (0, -1))
        attempt = 0
        while time.monotonic() < deadline:
            await self.spawn_husk_near_bot(offset=offsets[attempt % len(offsets)])
            attempt += 1
            attempt_deadline = min(deadline, time.monotonic() + 8.0)
            while time.monotonic() < attempt_deadline:
                state = await self._read_bot_state()
                if not state.missing and state.health < baseline.health:
                    self._emit_fixture_event(
                        "scenario_husk_attack_observed",
                        health=state.health,
                        attempt=attempt,
                    )
                    return
                await asyncio.sleep(0.25)
            await self.clear_hostiles()
        raise TimeoutError("fixture husk did not damage the bot")

    async def clear_hostiles(self) -> None:
        await asyncio.to_thread(
            self._rcon.request,
            "kill @e[type=minecraft:husk]",
        )
        self._emit_fixture_event("scenario_hostiles_cleared")

    async def cleanup(self) -> None:
        if self._chat_sender_ready:
            await asyncio.to_thread(
                self._rcon.request,
                f"player {self.chat_sender} kill",
            )
            self._chat_sender_ready = False

    async def _spawn_chat_sender(self) -> None:
        state = await self._require_bot_state()
        x = round(state.pos[0]) + _FIXTURE_CHAT_DISTANCE
        y = round(state.pos[1])
        z = round(state.pos[2])
        await asyncio.to_thread(
            self._rcon.request,
            f"player {self.chat_sender} kill",
        )
        await asyncio.to_thread(
            self._rcon.request,
            f"player {self.chat_sender} spawn at {x} {y} {z}",
        )
        await self._wait_for_entity(self.chat_sender, timeout_s=10.0)
        await asyncio.to_thread(
            self._rcon.request,
            f"gamemode spectator {self.chat_sender}",
        )
        self._chat_sender_ready = True
        self._emit_fixture_event(
            "scenario_chat_sender_ready",
            sender=self.chat_sender,
            gamemode="spectator",
            position=[x, y, z],
            distance=_FIXTURE_CHAT_DISTANCE,
        )

    async def _wait_for_entity(self, name: str, *, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            response = await asyncio.to_thread(
                self._rcon.request,
                f"data get entity {name} Pos",
            )
            if response.strip() and "No entity was found" not in response:
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"fixture FakePlayer {name!r} did not join the world")

    async def _require_bot_state(self) -> _FixtureBotState:
        state = await self._read_bot_state()
        if state.missing:
            raise RuntimeError("cannot schedule fixture fact while Bot is missing")
        return state

    async def _read_bot_state(self) -> _FixtureBotState:
        position_response = await asyncio.to_thread(
            self._rcon.request,
            f"data get entity {self.bot_name} Pos",
        )
        if not position_response.strip() or "No entity was found" in position_response:
            return _FixtureBotState(True, (0.0, 0.0, 0.0), 0.0)
        position = _parse_entity_vector(position_response)
        health_response = await asyncio.to_thread(
            self._rcon.request,
            f"data get entity {self.bot_name} Health",
        )
        health = _parse_entity_scalar(health_response)
        return _FixtureBotState(False, position, health)

    async def _wait_for_production_record(
        self,
        predicate: Callable[[Mapping[str, object]], bool],
        *,
        timeout_s: float,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for record in self._production_records():
                if predicate(record):
                    return record
            await asyncio.sleep(0.1)
        raise TimeoutError(
            f"expected production trace record did not arrive: {self._production_trace_path}"
        )

    def _production_records(self) -> list[dict[str, object]]:
        return _read_trace_records(self._production_trace_path)

    def _emit_fixture_event(self, event: str, **fields: object) -> None:
        self._fixture_seq += 1
        record = sanitize_observation(
            {
                "fixture_seq": self._fixture_seq,
                "ts": time.time(),
                "session_id": "ag-external-fixture",
                "event": event,
                **fields,
            }
        )
        with self._fixture_trace_path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(record, sort_keys=True) + "\n")


def _parse_entity_vector(response: str) -> tuple[float, float, float]:
    match = re.search(r"\[([^\]]+)\]", response)
    if match is None:
        raise ValueError(f"entity position response is malformed: {response!r}")
    values = [
        float(re.sub(r"[dDfFbBsSlL]$", "", part.strip()))
        for part in match.group(1).split(",")
    ]
    if len(values) != 3:
        raise ValueError(f"entity position response is malformed: {response!r}")
    return values[0], values[1], values[2]


def _parse_entity_scalar(response: str) -> float:
    matches = re.findall(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?[dDfFbBsSlL]?",
        response,
    )
    if not matches:
        raise ValueError(f"entity scalar response is malformed: {response!r}")
    return float(re.sub(r"[dDfFbBsSlL]$", "", matches[-1]))


async def _first_segment(
    context: ExternalInteractiveScenarioContext,
    duration_s: float,
) -> None:
    started = time.monotonic()
    await context.emit_chat(FIXTURE_SENDER, "你好，你是谁？请简短说明你现在能做什么。")
    await asyncio.sleep(12)
    await context.emit_chat(FIXTURE_SENDER, f"/goal {MATERIAL_GOAL}")
    await asyncio.sleep(90)
    await context.emit_chat(FIXTURE_SENDER, "/pause gate_pause_coverage")
    await asyncio.sleep(20)
    await context.emit_chat(FIXTURE_SENDER, "/continue")
    await asyncio.sleep(max(0.0, duration_s - (time.monotonic() - started)))


async def _quality_segment(
    context: ExternalInteractiveScenarioContext,
    duration_s: float,
) -> None:
    started = time.monotonic()
    await context.emit_chat(FIXTURE_SENDER, f"/goal {MATERIAL_GOAL}")
    await asyncio.sleep(max(0.0, duration_s - (time.monotonic() - started)))
    await context.emit_chat(FIXTURE_SENDER, "/quit ag_quality_gate_complete")


async def _second_segment(
    context: ExternalInteractiveScenarioContext,
    duration_s: float,
) -> None:
    async def wait_until(offset_s: float) -> bool:
        remaining = offset_s - (time.monotonic() - started)
        if remaining <= 0:
            return True
        await asyncio.sleep(remaining)
        return time.monotonic() - started < duration_s

    try:
        started = time.monotonic()
        if await wait_until(15):
            await context.emit_chat(FIXTURE_SENDER, "请回忆刚才的目标和已经确认的世界事实，然后继续当前任务。")
        if await wait_until(75):
            await context.spawn_fake_player_near_bot(GUIDE_NAME, distance=6)
            await context.emit_chat(
                FIXTURE_SENDER,
                f"/goal 请找到并短暂跟随 {GUIDE_NAME}，保持安全距离；完成后如实汇报。",
            )
        if await wait_until(210):
            await context.emit_chat(FIXTURE_SENDER, f"/goal {MATERIAL_GOAL}")
        if await wait_until(360):
            await context.set_difficulty("normal")
            await context.spawn_husk_near_bot(distance=2)
        if await wait_until(_IDLE_PROMPT_OFFSET_S):
            await context.clear_hostiles()
            await context.set_difficulty("peaceful")
            idle_marker = await context.emit_chat(
                FIXTURE_SENDER,
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
            await context.emit_chat(FIXTURE_SENDER, f"/goal {MATERIAL_GOAL}")
        if await wait_until(max(0.0, duration_s - 80)):
            await context.emit_chat(FIXTURE_SENDER, "/cancel gate_cancellation_coverage")
        await asyncio.sleep(max(0.0, duration_s - (time.monotonic() - started)))
        await context.emit_chat(FIXTURE_SENDER, "/quit ag_gate_complete")
    finally:
        await context.clear_hostiles()
        await context.set_difficulty("peaceful")
        await context.remove_fake_player(GUIDE_NAME)


def _run_child(
    environment: Mapping[str, str],
    camera: bool,
    diagnostic_path: str,
) -> None:
    os.environ.clear()
    os.environ.update(environment)
    try:
        config = real_server_config_from_env()
        camera_path = discover_camera_config_path(environ=os.environ) if camera else None
        raise SystemExit(
            asyncio.run(
                run_real_server_interactive(
                    config,
                    None,
                    max_steps=None,
                    camera_config=camera_path,
                    scenario_hook=None,
                    terminal_goal=AG_FP30_GOAL,
                )
            )
        )
    except BaseException as exc:
        if not isinstance(exc, SystemExit) or int(exc.code or 0) != 0:
            payload = sanitize_observation(
                {
                    "segment": "production_child",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            Path(diagnostic_path).write_text(
                json.dumps(payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        raise


def _read_trace_records(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                events.append(record)
    return events


def _combined_trace_records(
    production_trace_path: Path,
    fixture_trace_path: Path | None,
) -> list[dict[str, object]]:
    records = [
        *(_read_trace_records(production_trace_path)),
        *(_read_trace_records(fixture_trace_path) if fixture_trace_path is not None else []),
    ]
    return sorted(
        records,
        key=lambda record: (
            float(record.get("ts"))
            if isinstance(record.get("ts"), (int, float))
            else float("inf"),
            1 if "fixture_seq" in record else 0,
            int(record.get("seq") or record.get("fixture_seq") or 0),
        ),
    )


def _provider_manifest_summary(
    production_events: list[dict[str, object]],
) -> dict[str, object]:
    manifests = [
        event for event in production_events if event.get("event") == "provider_manifest"
    ]
    valid = bool(manifests) and all(
        manifest.get("body_provider") == "java"
        and manifest.get("legacy_rcon_constructed") is False
        and manifest.get("legacy_scarpet_body_constructed") is False
        for manifest in manifests
    )
    return {
        "count": len(manifests),
        "valid": valid,
        "body_providers": sorted(
            {str(manifest.get("body_provider")) for manifest in manifests}
        ),
        "legacy_rcon_constructed": any(
            manifest.get("legacy_rcon_constructed") is not False
            for manifest in manifests
        ),
        "legacy_scarpet_body_constructed": any(
            manifest.get("legacy_scarpet_body_constructed") is not False
            for manifest in manifests
        ),
    }


def _trace_summary(
    production_trace_path: Path,
    *,
    fixture_trace_path: Path | None = None,
    active_elapsed_s: float | None = None,
) -> dict[str, object]:
    production_events = _read_trace_records(production_trace_path)
    fixture_events = (
        _read_trace_records(fixture_trace_path)
        if fixture_trace_path is not None
        else []
    )
    events = _combined_trace_records(production_trace_path, fixture_trace_path)
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
    secret_matches = sum(
        len(
            re.findall(
                r"\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,})\b",
                path.read_text(encoding="utf-8", errors="replace"),
            )
        )
        for path in (production_trace_path, fixture_trace_path)
        if path is not None and path.exists()
    )
    ready_to_terminal_elapsed_s = _trace_elapsed_s(events, "scenario_body_ready", "session_terminal")
    idle_window = _idle_window_summary(events)
    quality = evaluate_autonomy_quality(
        events,
        yardstick=AG_FP30_YARDSTICK,
        active_window_s=active_elapsed_s,
    )
    return {
        "trace_records": len(events),
        "production_trace_records": len(production_events),
        "fixture_trace_records": len(fixture_events),
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
        "provider_manifest": _provider_manifest_summary(production_events),
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
    provider_manifest_valid: bool,
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
        and provider_manifest_valid
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


def _production_environment(environment: Mapping[str, str]) -> dict[str, str]:
    child_environment = dict(environment)
    child_environment["MINEBOT_BODY_PROVIDER"] = "java"
    child_environment.setdefault("MINEBOT_JAVA_BODY_URL", _LOCAL_JAVA_BODY_URL)
    for key in _RCON_ENV_KEYS:
        child_environment.pop(key, None)
    return child_environment


def _fixture_rcon_config(environment: Mapping[str, str]) -> RconConfig:
    return RconConfig(
        host=str(environment["MINEBOT_REAL_RCON_HOST"]),
        port=int(environment["MINEBOT_REAL_RCON_PORT"]),
        password=str(environment["MINEBOT_REAL_RCON_PASSWORD"]),
        timeout_s=float(environment.get("MINEBOT_REAL_RCON_TIMEOUT", "20")),
    )


def _wait_for_interactive_ready(
    trace_path: Path,
    process: multiprocessing.Process,
    *,
    started_at: float,
    timeout_s: float,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any(
            record.get("event") == "interactive_ready"
            and isinstance(record.get("ts"), (int, float))
            and float(record["ts"]) >= started_at
            for record in _read_trace_records(trace_path)
        ):
            return True
        if process.exitcode is not None:
            return False
        time.sleep(0.1)
    return False


def _scenario_for_segment(
    segment: str,
    context: ExternalInteractiveScenarioContext,
    duration_s: float,
) -> Awaitable[None]:
    if segment == "quality":
        return _quality_segment(context, duration_s)
    if segment == "first":
        return _first_segment(context, duration_s)
    if segment == "second":
        return _second_segment(context, duration_s)
    raise ValueError(f"unknown AG gate segment: {segment!r}")


async def _run_scenario_while_child_alive(
    process: multiprocessing.Process,
    scenario: Awaitable[None],
) -> None:
    scenario_task = asyncio.create_task(scenario)
    try:
        while not scenario_task.done():
            if process.exitcode is not None:
                await asyncio.sleep(0.25)
                if not scenario_task.done():
                    raise RuntimeError(
                        f"production child exited during fixture scenario: {process.exitcode}"
                    )
            await asyncio.sleep(0.1)
        await scenario_task
    finally:
        if not scenario_task.done():
            scenario_task.cancel()
            try:
                await scenario_task
            except asyncio.CancelledError:
                pass


def _write_fixture_diagnostic(
    path: Path,
    *,
    segment: str,
    exc: BaseException,
) -> None:
    payload = sanitize_observation(
        {
            "segment": segment,
            "source": "external_fixture",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    )
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _run_segment(
    context: multiprocessing.context.BaseContext,
    production_environment: Mapping[str, str],
    fixture_environment: Mapping[str, str],
    segment: str,
    duration_s: float,
    *,
    camera: bool,
    hard_stop: bool,
    diagnostic_path: Path,
    production_trace_path: Path,
    fixture_trace_path: Path,
) -> SegmentResult:
    process = context.Process(
        target=_run_child,
        args=(dict(production_environment), camera, str(diagnostic_path)),
    )
    started = time.monotonic()
    started_at = time.time()
    process.start()
    if not _wait_for_interactive_ready(
        production_trace_path,
        process,
        started_at=started_at,
        timeout_s=_BODY_READY_TIMEOUT_S,
    ):
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

    fixture_failed = False
    ready_at: float | None = None
    rcon = RconClient(_fixture_rcon_config(fixture_environment))
    fixture = ExternalInteractiveScenarioContext(
        bot_name=str(fixture_environment["MINEBOT_REAL_BOT"]),
        chat_sender=FIXTURE_SENDER,
        rcon=rcon,
        production_trace_path=production_trace_path,
        fixture_trace_path=fixture_trace_path,
    )
    try:
        rcon.connect()
    except BaseException as exc:
        fixture_failed = True
        fixture._emit_fixture_event(
            "scenario_fixture_failed",
            stage="connect",
            error_type=type(exc).__name__,
            message=str(exc),
        )
        _write_fixture_diagnostic(
            diagnostic_path,
            segment=f"{segment}_connect",
            exc=exc,
        )
    if not fixture_failed:
        try:
            asyncio.run(fixture.wait_for_body_ready(timeout_s=60.0))
            ready_at = time.monotonic()
            asyncio.run(
                _run_scenario_while_child_alive(
                    process,
                    _scenario_for_segment(segment, fixture, duration_s),
                )
            )
        except BaseException as exc:
            fixture_failed = True
            fixture._emit_fixture_event(
                "scenario_fixture_failed",
                error_type=type(exc).__name__,
                message=str(exc),
            )
            _write_fixture_diagnostic(diagnostic_path, segment=segment, exc=exc)
        finally:
            try:
                asyncio.run(fixture.cleanup())
            except BaseException as cleanup_exc:
                fixture_failed = True
                fixture._emit_fixture_event(
                    "scenario_fixture_failed",
                    stage="cleanup",
                    error_type=type(cleanup_exc).__name__,
                    message=str(cleanup_exc),
                )
                _write_fixture_diagnostic(
                    diagnostic_path,
                    segment=f"{segment}_cleanup",
                    exc=cleanup_exc,
                )
            finally:
                rcon.close()

    if ready_at is None:
        ready_at = time.monotonic()
    if fixture_failed and process.is_alive():
        process.terminate()
        process.join(timeout=30)
    process.join(timeout=0.0 if hard_stop else _SECOND_SEGMENT_EXIT_GRACE_S)
    terminated_at_deadline = False
    if process.is_alive():
        terminated_at_deadline = hard_stop and not fixture_failed
        process.terminate()
        process.join(timeout=30)
    return SegmentResult(
        exit_code=process.exitcode or 0,
        elapsed_s=round(time.monotonic() - started, 3),
        active_elapsed_s=round(time.monotonic() - ready_at, 3),
        ready_elapsed_s=round(ready_at - started, 3),
        body_ready=not fixture_failed,
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
    production_trace_path = run_dir / "trace.jsonl"
    fixture_trace_path = run_dir / "fixture-trace.jsonl"
    environment["MINEBOT_BODY_PROVIDER"] = "java"
    environment["MINEBOT_AGENT_LOG_PATH"] = str(production_trace_path)
    environment["MINEBOT_AGENT_STATE_DB"] = str(run_dir / "state.sqlite3")
    production_environment = _production_environment(environment)
    preflight_runtime_environment(production_environment, camera=not args.no_camera)
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
            production_environment,
            environment,
            "quality",
            args.duration_seconds,
            camera=not args.no_camera,
            hard_stop=False,
            diagnostic_path=diagnostic,
            production_trace_path=production_trace_path,
            fixture_trace_path=fixture_trace_path,
        )
        final_camera_cleanup = _stop_gate_camera(camera_path)
        trace_summary = _trace_summary(
            production_trace_path,
            fixture_trace_path=fixture_trace_path,
            active_elapsed_s=quality_segment.active_elapsed_s,
        )
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
            "trace_paths": {
                "production": str(production_trace_path),
                "fixture": str(fixture_trace_path),
            },
            "production_rcon_env_present": any(
                key in production_environment for key in _RCON_ENV_KEYS
            ),
            "trace": trace_summary,
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
        provider_manifest = report["trace"].get("provider_manifest")
        return 0 if _quality_gate_passes(
            quality_segment,
            quality,
            active_duration_met=bool(report["coverage"].get("active_duration_met")),
            provider_manifest_valid=(
                isinstance(provider_manifest, dict)
                and provider_manifest.get("valid") is True
            ),
        ) else 1

    first_diagnostic = run_dir / "first-segment-diagnostic.json"
    first = _run_segment(
        process_context,
        production_environment,
        environment,
        "first",
        args.restart_after_seconds,
        camera=not args.no_camera,
        hard_stop=True,
        diagnostic_path=first_diagnostic,
        production_trace_path=production_trace_path,
        fixture_trace_path=fixture_trace_path,
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
            "trace_paths": {
                "production": str(production_trace_path),
                "fixture": str(fixture_trace_path),
            },
            "production_rcon_env_present": any(
                key in production_environment for key in _RCON_ENV_KEYS
            ),
            "trace": _trace_summary(
                production_trace_path,
                fixture_trace_path=fixture_trace_path,
                active_elapsed_s=first.active_elapsed_s,
            ),
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
        production_environment,
        environment,
        "second",
        second_duration,
        camera=not args.no_camera,
        hard_stop=False,
        diagnostic_path=second_diagnostic,
        production_trace_path=production_trace_path,
        fixture_trace_path=fixture_trace_path,
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
        "trace_paths": {
            "production": str(production_trace_path),
            "fixture": str(fixture_trace_path),
        },
        "production_rcon_env_present": any(
            key in production_environment for key in _RCON_ENV_KEYS
        ),
        "trace": _trace_summary(
            production_trace_path,
            fixture_trace_path=fixture_trace_path,
            active_elapsed_s=active_elapsed_s,
        ),
        "camera": {
            "initial": camera_initial,
            "after_first_segment": first_camera_cleanup,
            "after_gate": final_camera_cleanup,
        },
    }
    (run_dir / "gate-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"AG gate report: {run_dir / 'gate-report.json'}")
    terminal_ok = bool(report["trace"].get("authoritative_satisfied"))
    provider_manifest = report["trace"].get("provider_manifest")
    provider_ok = bool(
        isinstance(provider_manifest, dict)
        and provider_manifest.get("valid") is True
    )
    return 0 if (
        first.body_ready
        and second.body_ready
        and first.exit_code in {-15, 0}
        and second.exit_code == 0
        and terminal_ok
        and provider_ok
    ) else 1


def _read_diagnostic(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
