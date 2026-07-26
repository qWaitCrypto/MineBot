#!/usr/bin/env python3
"""End-to-end long-run harness for the FakePlayer Java Body.

Runs a sustained (target 30-60 min) autonomous session that drives the Java
Body through its agent tools over the live test server, emits the frozen
autonomy-quality evaluator's trace vocabulary continuously, and reports the
three-signal verdict — effective output, process health, recovery. This is
the gate's judge, not a rigid pass/fail assertion.

Two drivers decide WHAT each step:

- ``model``  — a real LLM chooses objectives through the Java tools. This is
  the formal AG-style run; it needs a valid model provider. It does NOT run
  without one (no fabricated key, no silent fallback).
- ``scripted`` — a deterministic objective sequence. A scripted run is
  DIRECTED BODY mechanism evidence: it validates the whole pipeline and the
  Body's sustained output/health/recovery, but per the closure rules it is
  NOT a substitute for the model Agent composition gate. The report is
  labelled accordingly.

The Body is driven only through the shared registry tools; every mutation is
answered by the real production governance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minebot.app.autonomy_quality import (  # noqa: E402
    AG_FP30_YARDSTICK,
    evaluate_autonomy_quality,
)
from minebot.app.java_body_tools import register_java_body_tools  # noqa: E402
from minebot.app.java_body_trace import body_progress_event  # noqa: E402
from minebot.brain.registry import ToolRegistry  # noqa: E402
from minebot.contract.governance import Region  # noqa: E402
from minebot.game.governance import GovernancePolicy  # noqa: E402
from minebot.game.java_body_adapter import (  # noqa: E402
    GovernanceAnswerer,
    JavaBodyClient,
    websocket_transport,
)
from minebot.game.rcon import RconClient, RconConfig  # noqa: E402

LOG_FAMILY = [
    "minecraft:oak_log",
    "minecraft:spruce_log",
    "minecraft:birch_log",
    "minecraft:jungle_log",
    "minecraft:acacia_log",
    "minecraft:dark_oak_log",
]
GATHER_FAMILIES = {
    "logs": LOG_FAMILY,
    "dirt": ["minecraft:dirt", "minecraft:grass_block", "minecraft:coarse_dirt"],
    "stone": ["minecraft:stone", "minecraft:cobblestone", "minecraft:andesite", "minecraft:diorite"],
    "sand": ["minecraft:sand", "minecraft:gravel"],
}
BODY_STATE_SAMPLE_GAP_S = 45.0  # well under the frozen 240s coverage gap


class TraceRecorder:
    """Accumulates evaluator trace events with a monotonic ts/seq."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self._events: list[dict] = []
        self._seq = 0

    def _next(self) -> tuple[float, int]:
        self._seq += 1
        return self._clock(), self._seq

    def ready(self) -> None:
        ts, seq = self._next()
        self._events.append({"event": "scenario_body_ready", "ts": ts, "seq": seq})

    def body_state(self, inventory_counts: dict, pos: list[float] | None, selected=None, offhand=None) -> None:
        ts, seq = self._next()
        event = {
            "event": "body_state",
            "ts": ts,
            "seq": seq,
            "missing": pos is None,
            "inventory_counts": inventory_counts,
            "selected_item": selected,
            "offhand_item": offhand,
        }
        if pos is not None:
            event["position"] = pos
        self._events.append(event)

    def tool_invoke(self, tool: str, args: dict, tactic: str, call_id: str) -> None:
        ts, seq = self._next()
        self._events.append({
            "event": "tool_invoke",
            "ts": ts,
            "seq": seq,
            "tool": tool,
            "tool_call_id": call_id,
            "mutating": True,
            "args_hash": _args_hash(tool, args),
            "tactic_signature": tactic,
        })

    def tool_result(self, tool: str, args: dict, tactic: str, call_id: str, result) -> None:
        ts, seq = self._next()
        self._events.append({
            "event": "tool_result",
            "ts": ts,
            "seq": seq,
            "tool": tool,
            "tool_call_id": call_id,
            "mutating": True,
            "args_hash": _args_hash(tool, args),
            "tactic_signature": tactic,
            "success": result.success,
            "reason": result.reason,
        })
        progress = body_progress_event(result, ts=ts, seq=seq)
        if progress is not None:
            self._events.append(progress)

    def terminal(self, body_owner, pending: int) -> None:
        ts, seq = self._next()
        self._events.append({
            "event": "session_terminal",
            "ts": ts,
            "seq": seq,
            "terminal_truth": {"facts": {"body_owner": body_owner, "pending_action_count": pending}},
        })

    @property
    def events(self) -> list[dict]:
        return self._events


def _args_hash(tool: str, args: dict) -> str:
    raw = tool + "|" + "&".join(f"{k}={args[k]}" for k in sorted(args))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _parse_inventory(raw: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for count, item in re.findall(r'count:\s*(\d+)[^}]*?id:\s*"([^"]+)"', raw):
        counts[item] = counts.get(item, 0) + int(count)
    for item, count in re.findall(r'id:\s*"([^"]+)"[^}]*?count:\s*(\d+)', raw):
        counts.setdefault(item, int(count))
    return counts


def _parse_pos(raw: str) -> list[float] | None:
    nums = re.findall(r"(-?\d+\.?\d*)d", raw)
    return [float(v) for v in nums[:3]] if len(nums) >= 3 else None


class ScriptedDriver:
    """Deterministic gather objectives that deliberately vary terrain so the
    Body meets natural obstacles and must recover by switching tactic."""

    def __init__(self, start: list[float]) -> None:
        self._start = start
        self._step = 0
        # A ring of explore anchors around spawn to keep the frontier moving.
        self._anchors = [(60, -50), (-60, 40), (40, 60), (-50, -55), (70, 20), (-30, 70)]

    def next_objective(self, ctx: dict) -> tuple[str, dict, str]:
        self._step += 1
        phase = self._step % 4
        if phase in (0, 1):
            family = "logs"
            return "collect_block", {"block_types": GATHER_FAMILIES[family], "search_radius": 40}, f"collect:{family}"
        if phase == 2:
            fam = ["dirt", "stone", "sand"][self._step % 3]
            return "collect_block", {"block_types": GATHER_FAMILIES[fam], "search_radius": 32}, f"collect:{fam}"
        ax, az = self._anchors[self._step % len(self._anchors)]
        return "navigate_to", {"x": int(self._start[0]) + ax, "z": int(self._start[2]) + az, "kind": "xz"}, f"explore:{ax}_{az}"


def _sample_body_state(rcon: RconClient, bot: str, recorder: TraceRecorder) -> None:
    pos = _parse_pos(rcon.command(f"data get entity {bot} Pos"))
    inv = _parse_inventory(rcon.command(f"data get entity {bot} Inventory"))
    recorder.body_state(inv, pos)


def _bot_alive(rcon: RconClient, bot: str) -> bool:
    return "data" in rcon.command(f"data get entity {bot} Pos")


def _ensure_alive(rcon: RconClient, bot: str, start: str | None) -> bool:
    """Keep the FakePlayer present for the long run. Returns True if a respawn
    was needed (an honest death/despawn to record)."""
    if _bot_alive(rcon, bot):
        return False
    rcon.command(f"player {bot} spawn")
    time.sleep(2)
    rcon.command(f"gamemode survival {bot}")
    if start:
        rcon.command(f"tp {bot} {start}")
        time.sleep(3)
    return True


def run(args: argparse.Namespace) -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=args.rcon_port, password=args.rcon_password))
    rcon.connect()
    rcon.command(f"player {args.bot} spawn")
    time.sleep(2)
    rcon.command(f"gamemode survival {args.bot}")
    if args.start:
        rcon.command(f"tp {args.bot} {args.start}")
        time.sleep(4)
    start_pos = _parse_pos(rcon.command(f"data get entity {args.bot} Pos")) or [0.0, 70.0, 0.0]

    natural = Region("longrun-natural", (-512, 0, -512), (512, 320, 512))
    governance = GovernanceAnswerer(GovernancePolicy(natural_regions=[natural]))
    client = JavaBodyClient(args.bot, websocket_transport(args.url), governance,
                            action_wall_timeout_s=args.action_timeout_s, recv_timeout_s=2.0)
    client.connect()
    registry = ToolRegistry()
    register_java_body_tools(registry, client)

    driver = _build_model_driver(start_pos) if args.driver == "model" else ScriptedDriver(start_pos)

    t0 = time.time()
    clock = lambda: time.time() - t0
    recorder = TraceRecorder(clock)
    recorder.ready()
    _sample_body_state(rcon, args.bot, recorder)

    last_sample = time.time()
    call_no = 0
    tool_calls = 0
    successes = 0
    respawns = 0
    consecutive_fail = 0
    print(f"[longrun] driver={args.driver} target={args.duration_s}s start={start_pos}", flush=True)
    while time.time() - t0 < args.duration_s:
        # Upkeep: a drowned/despawned FakePlayer is respawned so the long run
        # continues; each respawn is an honest death recorded below.
        if _ensure_alive(rcon, args.bot, args.start):
            respawns += 1
            _sample_body_state(rcon, args.bot, recorder)
            print(f"[longrun] {round(time.time()-t0)}s RESPAWN (death #{respawns})", flush=True)

        tool, params, tactic = driver.next_objective({"elapsed": time.time() - t0})
        call_no += 1
        call_id = f"c{call_no}"
        recorder.tool_invoke(tool, params, tactic, call_id)
        try:
            result = registry.get(tool).callable(params)
        except Exception as error:  # noqa: BLE001 — keep the long run alive
            from minebot.contract import ToolResult
            result = ToolResult(success=False, reason=f"harness_error:{type(error).__name__}", can_retry=True)
        recorder.tool_result(tool, params, tactic, call_id, result)
        if hasattr(driver, "report_result"):
            driver.report_result(tool, params, result)
        tool_calls += 1
        if result.success:
            successes += 1
            consecutive_fail = 0
        else:
            consecutive_fail += 1

        if time.time() - last_sample >= BODY_STATE_SAMPLE_GAP_S:
            _sample_body_state(rcon, args.bot, recorder)
            last_sample = time.time()
        print(f"[longrun] {round(time.time()-t0)}s call#{call_no} {tool} {tactic} -> "
              f"{'ok' if result.success else 'fail'}/{result.reason}", flush=True)

        # Pace the loop so instant failures cannot spin into the transport rate
        # limit: a floor delay always, a longer cooldown after transport-ish
        # failures, and a backoff-plus-reconnect circuit breaker on a streak.
        reason = str(result.reason)
        if result.success:
            time.sleep(args.pace_s)
        elif reason in ("rate_limited", "collect_no_terminal", "navigate_no_terminal") or reason.startswith("action_reconciliation"):
            time.sleep(max(2.0, args.pace_s))
        else:
            time.sleep(args.pace_s)
        if consecutive_fail and consecutive_fail % 8 == 0:
            print(f"[longrun] circuit breaker: {consecutive_fail} consecutive failures, cooling down", flush=True)
            time.sleep(5.0)
            _ensure_alive(rcon, args.bot, args.start)

    _sample_body_state(rcon, args.bot, recorder)
    recorder.terminal(body_owner=None, pending=0)
    client.close()

    report = evaluate_autonomy_quality(recorder.events, yardstick=AG_FP30_YARDSTICK,
                                       active_window_s=args.active_window_s or None)
    artifact = {
        "scope": "java_body_longrun",
        "formal_gate": args.driver == "model",
        "driver": args.driver,
        "directed_body_evidence": args.driver != "model",
        "duration_s": round(time.time() - t0, 1),
        "tool_calls": tool_calls,
        "successes": successes,
        "respawns": respawns,
        "model_calls": getattr(driver, "model_calls", 0),
        "model_fallbacks": getattr(driver, "model_failures", 0),
        "start_pos": start_pos,
        "verdict": report["verdict"],
        "signals": {
            "effective_output": report["signals"]["effective_output"].get("verdict"),
            "process_health": report["signals"]["process_health"].get("verdict"),
            "recovery": report["signals"]["recovery"].get("verdict"),
        },
        "output_points": report["signals"]["effective_output"].get("points"),
        "coverage_verdict": report["coverage"]["verdict"],
        "report": report,
        "trace_events": len(recorder.events),
    }
    out = Path(args.out)
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps({k: artifact[k] for k in (
        "driver", "formal_gate", "duration_s", "tool_calls", "successes",
        "verdict", "signals", "output_points", "coverage_verdict")}, indent=2))
    print(f"[longrun] artifact -> {out}")
    return 0


# Responses-API function tools (flat shape: no nested "function" wrapper).
_LLM_TOOLS = [
    {
        "type": "function",
        "name": "collect_block",
        "description": "Search for and collect one block of any listed type near the bot "
                       "(mines it under governance and verifies the inventory gain).",
        "parameters": {
            "type": "object",
            "properties": {
                "block_types": {"type": "array", "items": {"type": "string"},
                                "description": "exact namespaced block ids, e.g. minecraft:oak_log, minecraft:stone"},
                "search_radius": {"type": "integer", "minimum": 4, "maximum": 64},
            },
            "required": ["block_types"],
        },
    },
    {
        "type": "function",
        "name": "navigate_to",
        "description": "Walk or swim toward a target column to explore or reposition. "
                       "kind='xz' targets a far column; 'near' ends within range of a cell.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "z": {"type": "integer"},
                "kind": {"type": "string", "enum": ["xz", "near"]},
                "y": {"type": "integer"},
                "range": {"type": "number"},
            },
            "required": ["x", "z"],
        },
    },
]

_LLM_SYSTEM = (
    "You are the strategic brain of a Minecraft survival bot. Its body can only "
    "navigate and collect blocks so far. Keep the bot productive for the whole "
    "session: gather resources and, when a target is unreachable or exhausted, "
    "switch to a different resource or explore a new area to recover. Always "
    "respond with exactly one tool call, never plain text. Use EXACT namespaced "
    "block ids only, for example minecraft:oak_log, minecraft:birch_log, "
    "minecraft:stone, minecraft:dirt, minecraft:sand, minecraft:gravel. Vary "
    "your objectives so you keep making progress."
)

_BLOCK_ALIAS = {
    "oak logs": "minecraft:oak_log", "oak log": "minecraft:oak_log", "wood": "minecraft:oak_log",
    "logs": "minecraft:oak_log", "log": "minecraft:oak_log", "stone": "minecraft:stone",
    "dirt": "minecraft:dirt", "sand": "minecraft:sand", "gravel": "minecraft:gravel",
    "grass": "minecraft:grass_block",
}


def _normalize_block_id(value: str) -> str:
    v = str(value).strip().lower()
    if ":" in v:
        return v
    if v in _BLOCK_ALIAS:
        return _BLOCK_ALIAS[v]
    return "minecraft:" + v.replace(" ", "_")


class ModelDriver:
    """Real LLM (Responses API) tool-use loop deciding the next Java Body
    objective, with multi-turn input threading so the model stays aware of
    what happened. Retry/backoff degrades a flaky gateway gracefully: if the
    model cannot be reached for a decision, a recorded fallback keeps the
    session alive rather than stalling."""

    def __init__(self, model: str, base_url: str, api_key: str, start_pos: list[float]) -> None:
        self._model = model
        self._url = base_url.rstrip("/") + "/v1/responses"
        self._key = api_key
        self._input = [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": f"You start near {start_pos}. Begin gathering resources; "
                                        f"decide your first objective."},
        ]
        self._fallback = ScriptedDriver(start_pos)
        self._pending_call_id = None
        self.model_calls = 0
        self.model_failures = 0

    def report_result(self, tool: str, params: dict, result) -> None:
        summary = f"{tool} -> {'ok' if result.success else 'fail'}/{result.reason}"
        if result.metrics and result.metrics.get("inventory_delta"):
            summary += f" delta={result.metrics['inventory_delta']}"
        if self._pending_call_id is not None:
            self._input.append({"type": "function_call_output", "call_id": self._pending_call_id, "output": summary})
            self._pending_call_id = None
        else:
            self._input.append({"role": "user", "content": summary})
        # Bound the threaded context: keep the two seed items + last 20 items.
        if len(self._input) > 24:
            self._input = self._input[:2] + self._input[-20:]

    def next_objective(self, ctx: dict) -> tuple[str, dict, str]:
        decision = self._decide()
        if decision is None:
            self.model_failures += 1
            tool, params, _ = self._fallback.next_objective(ctx)
            return tool, params, f"fallback:{tool}"
        call_id, tool, params = decision
        self._pending_call_id = call_id
        if tool == "collect_block":
            params["block_types"] = [_normalize_block_id(b) for b in params.get("block_types") or ["minecraft:oak_log"]]
            tactic = "collect:" + ",".join(params["block_types"])[:40]
        else:
            tactic = f"navigate:{params.get('x')}_{params.get('z')}"
        return tool, params, tactic

    def _decide(self):
        import urllib.request

        body = json.dumps({
            "model": self._model,
            "input": self._input,
            "tools": _LLM_TOOLS,
            "tool_choice": "required",
        }).encode()
        for _attempt in range(5):
            self.model_calls += 1
            req = urllib.request.Request(self._url, data=body, headers={
                "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode())
                for item in data.get("output", []):
                    if item.get("type") == "function_call" and item.get("name") in ("collect_block", "navigate_to"):
                        self._input.append(item)  # thread the call back for context
                        args = json.loads(item.get("arguments") or "{}")
                        return item.get("call_id"), item.get("name"), args
                return None
            except Exception:  # noqa: BLE001 — flaky gateway; retry then fall back
                time.sleep(3)
        return None


def _build_model_driver(start_pos: list[float]) -> "ModelDriver":
    import os

    key = os.environ.get("MINEBOT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("MINEBOT_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    model = os.environ.get("MINEBOT_LLM_MODEL", "gpt-5.6-luna")
    if not key or not base:
        print("[longrun] model driver needs MINEBOT_LLM_API_KEY + MINEBOT_LLM_BASE_URL (or OPENAI_*). Aborting.",
              file=sys.stderr)
        raise SystemExit(3)
    return ModelDriver(model, base, key, start_pos)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--driver", choices=("scripted", "model"), default="scripted")
    parser.add_argument("--duration-s", type=float, default=1800.0, dest="duration_s")
    parser.add_argument("--active-window-s", type=float, default=0.0, dest="active_window_s")
    parser.add_argument("--url", default="ws://127.0.0.1:8767")
    parser.add_argument("--bot", default="JavaBodyLongrun")
    parser.add_argument("--start", default="58 72 -52")
    parser.add_argument("--pace-s", type=float, default=0.5, dest="pace_s")
    parser.add_argument("--rcon-port", type=int, default=25576, dest="rcon_port")
    parser.add_argument("--rcon-password", default="test", dest="rcon_password")
    parser.add_argument("--action-timeout-s", type=float, default=90.0, dest="action_timeout_s")
    parser.add_argument("--out", default="logs/agentic-runtime/java-body-longrun-20260727.json")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
