#!/usr/bin/env python3
"""Bounded Q4t Part 2 probe for server-load attribution.

The probe keeps one real Body session and aligns four independent observations
on the same wall-clock timeline:

* Scarpet navigation segment diagnostics (expansions and segment bursts),
* ``tick query`` MSPT/percentiles,
* server-log ``Can't keep up`` and RCON-close lines, and
* Camera bridge status (loaded chunks and frame telemetry when attached).

It is intentionally short and uses pure movement navigation, so it is not a
Q4 rehearsal or a Q5 gate attempt.  Missing Camera telemetry is recorded as
missing; it is never treated as evidence of zero Camera load.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.body.navigation import (  # noqa: E402
    NavigationRunConfig,
    NavigationTransactions,
    load_limited_navigation_config,
    pure_movement_navigation_config,
)
from minebot.contract import LocalProgressController  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.navigation import GoalNear  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SERVER_LOG = ROOT / "test-server" / "logs" / "latest.log"
CAMERA_ENDPOINT = "ws://127.0.0.1:8766"
CAMERA_OBSERVER_ID = "e3be19c1-6923-3226-8108-2df310ddff82"
BOT = "Bot1"
KEEP_UP_RE = re.compile(
    r"Can't keep up! Is the server overloaded\? Running (?P<ms>\d+)ms or (?P<ticks>\d+) ticks behind"
)
RCON_CLOSED_RE = re.compile(r"RCON socket closed")
MSPT_RE = re.compile(
    r"Average time per tick:\s*(?P<avg>[0-9.]+)ms.*?P95:\s*(?P<p95>[0-9.]+)ms.*?P99:\s*(?P<p99>[0-9.]+)ms",
    re.DOTALL,
)


def _command(rcon: RconClient, command: str, *, delay_s: float = 0.05) -> str:
    result = rcon.command(command)
    if delay_s:
        time.sleep(delay_s)
    return result


def _log_offset() -> int:
    try:
        return SERVER_LOG.stat().st_size
    except OSError:
        return 0


def _read_log_since(offset: int) -> list[str]:
    try:
        with SERVER_LOG.open("rb") as handle:
            handle.seek(max(0, offset))
            return handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _parse_keep_up(lines: list[str]) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for line in lines:
        match = KEEP_UP_RE.search(line)
        if match:
            observations.append(
                {
                    "line": line[-500:],
                    "running_ms": int(match.group("ms")),
                    "ticks_behind": int(match.group("ticks")),
                }
            )
    return observations


def _parse_rcon_closes(lines: list[str]) -> list[str]:
    return [line[-500:] for line in lines if RCON_CLOSED_RE.search(line)]


def _tick_query(rcon: RconClient) -> dict[str, object]:
    raw = _command(rcon, "tick query", delay_s=0)
    match = MSPT_RE.search(raw)
    if not match:
        return {"raw": raw[-1000:], "parsed": False}
    return {
        "raw": raw[-1000:],
        "parsed": True,
        "average_ms": float(match.group("avg")),
        "p95_ms": float(match.group("p95")),
        "p99_ms": float(match.group("p99")),
    }


async def _camera_status_async() -> dict[str, object]:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - environment dependent
        return {"available": False, "error_type": type(exc).__name__, "error": str(exc)}

    counter = 0

    async with websockets.connect(CAMERA_ENDPOINT, max_size=32_768, proxy=None) as websocket:
        async def request(request_type: str, **fields: object) -> dict[str, object]:
            nonlocal counter
            counter += 1
            request_id = f"q4t-{uuid4().hex[:10]}-{counter}"
            payload = {
                "channel": "observer-control",
                "type": request_type,
                "request_id": request_id,
                **fields,
            }
            await websocket.send(json.dumps(payload, separators=(",", ":")))
            response = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3.0))
            if not isinstance(response, dict):
                raise RuntimeError("Camera bridge returned a non-object response")
            if response.get("type") == "ERROR":
                raise RuntimeError(
                    f"{response.get('code')}: {response.get('message')}"
                )
            if response.get("request_id") != request_id:
                raise RuntimeError("Camera bridge response request_id mismatch")
            return response

        await request("HELLO", protocol="observer-control/1")
        response = await request("STATUS")
        observer = response.get("observer")
        server = response.get("server")
        telemetry = response.get("client_telemetry")
        return {
            "available": True,
            "state": response.get("state"),
            "mode": response.get("mode"),
            "observer": observer if isinstance(observer, dict) else {},
            "server": server if isinstance(server, dict) else {},
            "client_telemetry": telemetry if isinstance(telemetry, dict) else {},
        }


def _camera_status() -> dict[str, object]:
    try:
        return asyncio.run(_camera_status_async())
    except Exception as exc:
        return {"available": False, "error_type": type(exc).__name__, "error": str(exc)}


def _snapshot(rcon: RconClient) -> dict[str, object]:
    return {
        "ts": time.time(),
        "tick_query": _tick_query(rcon),
        "camera": _camera_status(),
    }


def _segment_facts(result: object) -> list[dict[str, object]]:
    metrics = getattr(result, "metrics", None)
    if not isinstance(metrics, dict):
        return []
    raw_segments = metrics.get("segments")
    if not isinstance(raw_segments, list):
        return []
    facts: list[dict[str, object]] = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        diagnostics = segment.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        facts.append(
            {
                "index": segment.get("index"),
                "status": segment.get("status"),
                "reason": segment.get("terminal_reason"),
                "expanded": diagnostics.get("expanded"),
                "event": diagnostics.get("event"),
                "waypoints": diagnostics.get("waypoints"),
                "elapsed": segment.get("elapsed_s"),
            }
        )
    return facts


def _classify(
    *,
    segment_facts: list[dict[str, object]],
    snapshots: list[dict[str, object]],
    keep_up: list[dict[str, object]],
    rcon_closes: list[str],
    max_expand: int,
) -> tuple[str, str, str]:
    expanded = [
        int(item["expanded"])
        for item in segment_facts
        if isinstance(item.get("expanded"), (int, float))
    ]
    high_expansion = bool(expanded and max(expanded) >= int(max_expand * 0.85))
    p95_values = [
        float(snapshot["tick_query"]["p95_ms"])
        for snapshot in snapshots
        if isinstance(snapshot.get("tick_query"), dict)
        and isinstance(snapshot["tick_query"].get("p95_ms"), (int, float))
    ]
    high_tick = bool(p95_values and max(p95_values) >= 50.0)
    if high_expansion and (keep_up or rcon_closes or high_tick):
        return (
            "self_induced",
            "A* expansion reached the bounded segment ceiling while the same probe observed tick pressure/fault lines.",
            "perform bounded FakePlayer-side load reduction before setting Q5 budget",
        )
    if (keep_up or rcon_closes) and not high_expansion:
        return (
            "transient",
            "Transport/server pressure was observed without a matching high-expansion segment in this session.",
            "retain normal Body budgets and repeat the bounded fault/rehearsal evidence before Q5",
        )
    return (
        "unproven",
        "This short session did not provide a simultaneous high-expansion plus tick/fault correlation; missing Camera telemetry also remains explicit.",
        "repeat with Camera attached and a longer compound route; do not set Q5 budget from this probe",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-expand", type=int, default=2500)
    parser.add_argument(
        "--distance",
        type=int,
        default=32,
        help="axis distance for each bounded route (larger values exercise more A* work)",
    )
    parser.add_argument(
        "--load-limited",
        action="store_true",
        help="apply the provider-local resource approach load profile",
    )
    arguments = parser.parse_args()

    with connect_or_skip(RconConfig()) as rcon:
        log_start = _log_offset()
        for command in (
            "script unload minebot",
            "script load minebot global",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false",
            "gamerule doWeatherCycle false",
            "time set day",
            "weather clear",
            f"player {BOT} kill",
        ):
            _command(rcon, command)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (8, 72, 8), timeout_s=20.0)
        state = body.get_state()
        sx, sy, sz = (int(value) for value in state.pos)
        navigator = NavigationTransactions.server_side(
            body,
            None,
            progress=LocalProgressController(),
        )
        config = pure_movement_navigation_config(
            NavigationRunConfig(
                max_segments=4,
                segment_timeout_s=12.0,
                server_grid_radius=64,
                server_max_expand=arguments.max_expand,
                allow_swim=True,
                aquatic_traversal=True,
                recovery_attempts=0,
            )
        )
        if arguments.load_limited:
            config = load_limited_navigation_config(config)
        distance = max(4, arguments.distance)
        targets = (
            (sx + distance, sy, sz),
            (sx, sy, sz + distance),
            (sx - distance, sy, sz),
        )
        snapshots: list[dict[str, object]] = [_snapshot(rcon)]
        runs: list[dict[str, object]] = []
        all_segments: list[dict[str, object]] = []
        try:
            for target in targets:
                started = time.time()
                result = navigator.navigate_to(
                    GoalNear(target, radius=3),
                    config=config,
                    timeout_s=45.0,
                )
                finished = time.time()
                segments = _segment_facts(result)
                all_segments.extend(segments)
                snapshots.append(_snapshot(rcon))
                runs.append(
                    {
                        "target": list(target),
                        "started_ts": started,
                        "finished_ts": finished,
                        "elapsed_s": round(finished - started, 3),
                        "result": result.to_payload(),
                        "segments": segments,
                    }
                )
        finally:
            _command(rcon, f"player {BOT} kill", delay_s=0.2)

        log_lines = _read_log_since(log_start)
        keep_up = _parse_keep_up(log_lines)
        rcon_closes = _parse_rcon_closes(log_lines)
        classification, rationale, next_stop = _classify(
            segment_facts=all_segments,
            snapshots=snapshots,
            keep_up=keep_up,
            rcon_closes=rcon_closes,
            max_expand=config.server_max_expand,
        )
        report: dict[str, object] = {
            "scope": "Q4t_part2_load_alignment",
            "bounded": True,
            "bot": BOT,
            "start": [sx, sy, sz],
            "distance": distance,
            "targets": [list(target) for target in targets],
            "navigation_profile": "load_limited_pure_movement" if arguments.load_limited else "pure_movement",
            "server_grid_radius": config.server_grid_radius,
            "server_max_expand": config.server_max_expand,
            "runs": runs,
            "snapshots": snapshots,
            "server_log": {
                "path": str(SERVER_LOG),
                "bytes_start": log_start,
                "new_line_count": len(log_lines),
                "cant_keep_up": keep_up,
                "rcon_socket_closed": rcon_closes,
            },
            "classification": classification,
            "rationale": rationale,
            "next_stop": next_stop,
            "evidence_limits": [
                "Camera status is sampled through the bridge; null frame telemetry means the observer was not attached, not zero Camera load.",
                "The server log is read from the local test-server offset; remote production logs are not inferred.",
            ],
        }
        output = arguments.output or ROOT / "logs" / "agentic-runtime" / f"q4t-load-alignment-{int(time.time())}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
