"""Panel state: a deterministic fold over trace records.

``apply`` reads only the record it is given (no clocks, no IO).  Everything that
depends on "now" — elapsed timers, staleness, alerts, animation — is derived at
render time from ``PanelState`` plus the caller's ``now``, so following a live
trace and replaying an old one are the same computation.
"""

from __future__ import annotations

import copy
import json
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from minebot.panel.projection import project_progress

MOVE_PERMISSIONS = frozenset({"move", "dig_vertical"})
REQUEST_PHASES = ("prepare", "connect", "send", "wait", "stream")
PROCESS_NAMES = ("goal", "builder", "mine", "get", "explore", "follow", "farm", "elytra")
HISTORY_LIMIT = 600
LOG_LIMIT = 500


@dataclass
class Call:
    id: str
    tool: str = "?"
    source: str = ""
    permission: str = ""
    mutating: bool = False
    args: str = ""
    asynchronous: bool = False
    model_ts: float | None = None
    invoke_ts: float | None = None
    result_ts: float | None = None
    delivered_ts: float | None = None
    invoke_seq: int = 0
    result_seq: int = 0
    success: bool | None = None
    reason: str = ""
    can_retry: bool | None = None
    frames: int = 0
    last_frame_ts: float | None = None
    position: list[float] | None = None
    inventory_delta: dict[str, int] = field(default_factory=dict)
    edits: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    invoke: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self.invoke_ts is not None and self.result_ts is None

    @property
    def duration(self) -> float | None:
        if self.invoke_ts is None or self.result_ts is None:
            return None
        return self.result_ts - self.invoke_ts

    @property
    def group(self) -> str:
        return call_group(self.permission, self.mutating)


def call_group(permission: str, mutating: bool) -> str:
    if permission in MOVE_PERMISSIONS:
        return "move"
    if mutating:
        return "work"
    return "sense"


@dataclass
class Request:
    index: int
    started_ts: float
    phase: str = "prepare"
    phase_ts: float = 0.0
    key: str | None = None
    attempt: int = 0
    status: str = "running"
    ended_ts: float | None = None
    ttfb_s: float | None = None
    received_bytes: int = 0
    byte_idle_s: float = 0.0
    model: str = ""
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    error: str = ""
    phases: list[tuple[str, float]] = field(default_factory=list)

    def enter(self, phase: str, ts: float) -> None:
        if self.phase != phase or not self.phases:
            self.phase = phase
            self.phase_ts = ts
            self.phases.append((phase, ts))


@dataclass
class LogLine:
    ts: float
    layer: str
    label: str
    detail: str = ""
    tone: str = ""


@dataclass
class Pulse:
    ts: float
    kind: str
    group: str = ""
    ok: bool = True


@dataclass
class ToolStats:
    count: int = 0
    ok: int = 0
    failed: int = 0
    durations: list[float] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)


@dataclass
class PanelState:
    first_ts: float | None = None
    last_ts: float | None = None
    last_seq: int = 0
    events: int = 0
    unknown: Counter = field(default_factory=Counter)
    event_counts: Counter = field(default_factory=Counter)
    # session
    bot: str = ""
    server: str = ""
    world_id: str = ""
    body_provider: str = ""
    model_route: str = ""
    model: str = ""
    tools_count: int = 0
    terminal: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    # agent
    profile: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)
    request: Request | None = None
    requests: deque = field(default_factory=lambda: deque(maxlen=60))
    request_total: int = 0
    request_failed: int = 0
    compactions: int = 0
    ttfbs: deque = field(default_factory=lambda: deque(maxlen=200))
    context_tokens: int = 0
    usage: Counter = field(default_factory=Counter)
    cache_ratio: float | None = None
    speech: deque = field(default_factory=lambda: deque(maxlen=20))
    # tools
    calls: dict[str, Call] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    stats: dict[str, ToolStats] = field(default_factory=dict)
    source_counts: Counter = field(default_factory=Counter)
    # body
    body: dict[str, Any] = field(default_factory=dict)
    body_ts: float | None = None
    vitals: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LIMIT))
    trail: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LIMIT))
    entities: list[dict[str, Any]] = field(default_factory=list)
    inventory: dict[str, int] = field(default_factory=dict)
    inventory_baseline: dict[str, int] | None = None
    hotbar: list[Any] | None = None
    # baritone
    baritone: dict[str, Any] = field(default_factory=dict)
    baritone_ts: float | None = None
    baritone_call: str | None = None
    movements: deque = field(default_factory=lambda: deque(maxlen=60))
    path_event_log: deque = field(default_factory=lambda: deque(maxlen=40))
    progress_frames: deque = field(default_factory=lambda: deque(maxlen=64))
    # world and people
    world_edits: deque = field(default_factory=lambda: deque(maxlen=400))
    body_events: deque = field(default_factory=lambda: deque(maxlen=200))
    operator: deque = field(default_factory=lambda: deque(maxlen=50))
    reflex: deque = field(default_factory=lambda: deque(maxlen=50))
    supervision: dict[str, Any] = field(default_factory=dict)
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    pulses: deque = field(default_factory=lambda: deque(maxlen=32))

    def call(self, call_id: str | None) -> Call | None:
        return self.calls.get(call_id) if call_id else None

    def ensure_call(self, call_id: str, tool: str | None = None) -> Call:
        call = self.calls.get(call_id)
        if call is None:
            call = self.calls[call_id] = Call(id=call_id, tool=tool or "?")
            self.order.append(call_id)
        elif tool and call.tool == "?":
            call.tool = tool
        return call

    def running_calls(self) -> list[Call]:
        return [self.calls[key] for key in self.order[-16:] if self.calls[key].running]

    def finished_calls(self) -> list[Call]:
        return [self.calls[key] for key in self.order if self.calls[key].result_ts is not None]


# ----------------------------------------------------------------------------- helpers

def _ts(record: dict[str, Any]) -> float:
    try:
        return float(record.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _log(state: PanelState, ts: float, layer: str, label: str, detail: str = "", tone: str = "") -> None:
    state.log.append(LogLine(ts, layer, label, detail, tone))


def _request(state: PanelState, ts: float) -> Request:
    current = state.request
    if current is None or current.status != "running":
        state.request_total += 1
        current = Request(index=state.request_total, started_ts=ts, phase_ts=ts)
        current.phases.append(("prepare", ts))
        state.request = current
        state.requests.append(current)
    return current


def args_text(summary: Any) -> str:
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except ValueError:
            return summary
    if isinstance(summary, dict):
        return " ".join(f"{key}={_short(value, 28)}" for key, value in summary.items())
    return _short(summary)


def inventory_delta(before: Any, after: Any) -> dict[str, int]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return {}
    delta = {}
    for item in set(before) | set(after):
        try:
            change = int(after.get(item, 0) or 0) - int(before.get(item, 0) or 0)
        except (TypeError, ValueError):
            continue
        if change:
            delta[str(item)] = change
    return delta


def _merge_body(state: PanelState, ts: float, frame: dict[str, Any]) -> None:
    body = state.body
    for key, value in frame.items():
        if key in ("navigation", "processes", "path_events", "construction_progress", "entities_near",
                   "inventory_counts", "hotbar"):
            continue
        body[key] = value
    state.body_ts = ts
    if "pos" in frame:
        pos = frame["pos"]
        if not state.trail or state.trail[-1][1] != pos:
            state.trail.append((ts, pos))
    if any(key in frame for key in ("health", "food", "oxygen")):
        state.vitals.append((ts, body.get("health"), body.get("food"), body.get("oxygen")))
    if "entities_near" in frame:
        state.entities = list(frame["entities_near"])
    if "hotbar" in frame:
        state.hotbar = frame["hotbar"]
    inventory = frame.get("inventory_counts")
    if isinstance(inventory, dict):
        counts = {str(key): int(value or 0) for key, value in inventory.items() if isinstance(value, (int, float))}
        if state.inventory_baseline is None:
            state.inventory_baseline = dict(counts)
        state.inventory = counts


def _merge_baritone(state: PanelState, ts: float, frame: dict[str, Any], call_id: str | None) -> None:
    baritone = state.baritone
    if "processes" in frame:
        baritone["processes"] = dict(frame["processes"])
    for key in ("baritone_goal", "pathing", "builder_paused", "any_process_active", "allow_break",
                "allow_place", "allow_inventory", "last_path_event"):
        if key in frame:
            baritone[key] = frame[key]
    navigation = frame.get("navigation")
    if isinstance(navigation, dict) and navigation:
        baritone["navigation"] = dict(navigation)
        movement = navigation.get("movement")
        if movement and (not state.movements or state.movements[-1][1] != movement
                         or state.movements[-1][2] != navigation.get("src")):
            state.movements.append((ts, movement, navigation.get("src")))
    events = frame.get("path_events")
    if isinstance(events, dict):
        previous = baritone.get("path_events") or {}
        if call_id != state.baritone_call:
            previous = {}
        for name, count in events.items():
            for _ in range(max(0, int(count or 0) - int(previous.get(name, 0) or 0))):
                state.path_event_log.append((ts, name))
        baritone["path_events"] = dict(events)
    if "construction_progress" in frame:
        baritone["construction_progress"] = dict(frame["construction_progress"])
    state.baritone_ts = ts
    state.baritone_call = call_id


def _progress(state: PanelState, ts: float, call_id: str | None, frame: dict[str, Any], lite: bool) -> None:
    _merge_body(state, ts, frame)
    if call_id or frame.get("navigation") or frame.get("processes"):
        _merge_baritone(state, ts, frame, call_id)
    call = state.call(call_id)
    if call is not None:
        call.frames += 1
        call.last_frame_ts = ts
    state.progress_frames.append((ts, lite))


# ----------------------------------------------------------------------------- reducers

def _on_session(state: PanelState, record: dict[str, Any], ts: float) -> None:
    event = record["event"]
    if event == "interactive_ready":
        state.server = str(record.get("server") or state.server)
        state.body_provider = str(record.get("body_provider") or "")
        _log(state, ts, "session", "就绪", f"{state.body_provider} {state.server}")
    elif event == "runtime_scope":
        state.bot = str(record.get("bot_id") or state.bot)
        state.world_id = str(record.get("world_id") or state.world_id)
    elif event == "provider_manifest":
        state.model_route = str(record.get("default_route") or "")
    elif event == "tool_manifest":
        tools = record.get("tools")
        state.tools_count = len(tools) if isinstance(tools, (list, dict)) else 0
    elif event == "session_terminal":
        state.terminal = {key: record.get(key) for key in ("status", "message", "mode", "lifecycle")}
        _log(state, ts, "session", "会话结束", f"{record.get('status')} {_short(record.get('message'), 60)}",
             "fail" if record.get("status") == "failed" else "")
    elif event == "task_outcome":
        state.outcome = {key: record.get(key) for key in ("status", "summary", "evidence")}
        state.outcome["ts"] = ts
        _log(state, ts, "agent", "任务结论", f"{record.get('status')} {_short(record.get('summary'), 60)}",
             "warn" if record.get("status") != "completed" else "ok")


def _on_responses(state: PanelState, record: dict[str, Any], ts: float) -> None:
    event = record["event"]
    if event in ("responses_eof_cleanup", "responses_tool_started", "responses_tool_output"):
        return
    request = _request(state, ts) if event not in ("responses_terminal", "responses_failed") \
        else (state.request or _request(state, ts))
    if record.get("request_key"):
        request.key = str(record["request_key"])
    if isinstance(record.get("attempt"), int):
        request.attempt = record["attempt"]
    if event == "responses_context_counted":
        estimates = record.get("estimates") or {}
        if isinstance(estimates, dict):
            total = sum(int(estimates.get(key) or 0) for key in ("instructions", "tools", "visible_input"))
            if total:
                state.context_tokens = total
    elif event == "responses_http_attempt":
        request.enter("connect", ts)
    elif event == "responses_http_phase":
        phase = str(record.get("phase") or "")
        if phase.startswith(("connection.", "proxy.")):
            request.enter("connect", ts)
        elif phase.startswith("http11.send_request") or phase.startswith("http2.send_request"):
            request.enter("send", ts)
        elif "receive_response_headers" in phase:
            request.enter("wait", ts)
    elif event in ("responses_http_headers", "responses_http_open", "responses_created"):
        request.enter("wait", ts)
        if record.get("response_model"):
            request.model = state.model = str(record["response_model"])
    elif event in ("responses_first_byte", "responses_first_activity", "responses_stream_activity",
                   "responses_stream_summary"):
        if event == "responses_first_byte" and request.ttfb_s is None:
            request.ttfb_s = float(record.get("elapsed_s") or ts - request.started_ts)
            state.ttfbs.append(request.ttfb_s)
        if event != "responses_stream_summary":
            request.enter("stream", ts)
        if record.get("received_bytes") is not None:
            request.received_bytes = int(record["received_bytes"])
        if record.get("byte_idle_s") is not None:
            request.byte_idle_s = float(record["byte_idle_s"])
    elif event == "responses_compaction_started":
        request.enter("compact", ts)
        state.compactions += 1
        _log(state, ts, "agent", "上下文压缩", f"{record.get('before_items')} 条", "warn")
    elif event == "responses_compaction_finished":
        request.enter("prepare", ts)
        _log(state, ts, "agent", "压缩完成", f"{record.get('before_items')} → {record.get('after_items')} 条")
    elif event == "responses_compaction_failed":
        request.enter("prepare", ts)
        _log(state, ts, "agent", "压缩失败", _short(record.get("details"), 60), "fail")
    elif event == "responses_terminal":
        status = str(record.get("status") or "")
        request.status = "completed" if status == "completed" else "failed"
        request.ended_ts = ts
        request.enter("done" if request.status == "completed" else "failed", ts)
        if record.get("response_model"):
            request.model = state.model = str(record["response_model"])
        usage = record.get("usage") or {}
        if isinstance(usage, dict):
            request.input_tokens = int(usage.get("input_tokens") or 0)
            request.output_tokens = int(usage.get("output_tokens") or 0)
            request.cached_tokens = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
            request.reasoning_tokens = int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
            state.usage["input"] += request.input_tokens
            state.usage["cached"] += request.cached_tokens
            state.usage["output"] += request.output_tokens
            state.usage["reasoning"] += request.reasoning_tokens
            if request.input_tokens:
                state.context_tokens = request.input_tokens
        if isinstance(record.get("cache_read_ratio"), (int, float)):
            state.cache_ratio = float(record["cache_read_ratio"])
        if request.status != "completed":
            state.request_failed += 1
            request.error = str(record.get("reason") or record.get("error_code") or status)
            _log(state, ts, "agent", "请求失败", request.error, "fail")
    elif event == "responses_failed":
        request.status = "failed"
        request.ended_ts = ts
        request.error = _short(record.get("message") or record.get("error_type"), 80)
        request.enter("failed", ts)
        state.request_failed += 1
        _log(state, ts, "agent", "请求失败", request.error, "fail")


def _on_model_usage(state: PanelState, record: dict[str, Any], ts: float) -> None:
    """Token usage of one SDK-kernel model call (the Responses kernel reports it on its terminal)."""
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return
    input_tokens = int(usage.get("input_tokens") or 0)
    state.usage["input"] += input_tokens
    state.usage["cached"] += int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    state.usage["output"] += int(usage.get("output_tokens") or 0)
    state.usage["reasoning"] += int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
    if input_tokens:
        state.context_tokens = input_tokens
    if isinstance(record.get("cache_read_ratio"), (int, float)):
        state.cache_ratio = float(record["cache_read_ratio"])


def _on_tool(state: PanelState, record: dict[str, Any], ts: float) -> None:
    event = record["event"]
    call_id = str(record.get("tool_call_id") or f"seq-{record.get('seq')}")
    if event == "responses_tool_started":
        call = state.ensure_call(call_id, record.get("tool"))
        call.model_ts = call.model_ts or ts
        call.asynchronous = bool(record.get("asynchronous"))
        state.pulses.append(Pulse(ts, "emit"))
    elif event == "responses_tool_output":
        call = state.call(call_id)
        if call is not None:
            call.delivered_ts = ts
    elif event == "tool_decision_context":
        state.decision = {key: record.get(key) for key in ("situation", "lifecycle", "policy_tags",
                                                            "recent_tool_results", "last_known_body_state")}
        state.decision["ts"] = ts
    elif event == "tool_invoke":
        call = state.ensure_call(call_id, record.get("tool"))
        call.invoke_ts = ts
        call.invoke_seq = int(record.get("seq") or 0)
        call.source = str(record.get("source") or "")
        call.permission = str(record.get("permission") or "")
        call.mutating = bool(record.get("mutating"))
        call.args = args_text(record.get("arguments_summary"))
        call.invoke = record
        state.source_counts[call.source or "?"] += 1
        state.pulses.append(Pulse(ts, "invoke", call.group))
        _log(state, ts, "agent", f"→ {call.tool}", call.args, "")
    elif event == "tool_result":
        call = state.ensure_call(call_id, record.get("tool"))
        call.result_ts = ts
        call.result_seq = int(record.get("seq") or 0)
        full = record.get("full_result") if isinstance(record.get("full_result"), dict) else {}
        success = full.get("success", record.get("success"))
        call.success = bool(success) if success is not None else None
        call.reason = str(full.get("reason") or record.get("reason") or "")
        call.can_retry = full.get("canRetry")
        call.result = record
        metrics = full.get("metrics") if isinstance(full.get("metrics"), dict) else {}
        position = metrics.get("position")
        if isinstance(position, list) and len(position) == 3:
            call.position = [round(float(value), 2) for value in position]
        call.inventory_delta = inventory_delta(metrics.get("inventory_before"), metrics.get("inventory_after"))
        if metrics.get("navigation") or metrics.get("path_events"):
            frame = project_progress(metrics)
            frame.pop("inventory_counts", None)
            frame.pop("entities_near", None)
            _merge_baritone(state, ts, frame, call_id)
        stats = state.stats.setdefault(call.tool, ToolStats())
        stats.count += 1
        if call.success is False:
            stats.failed += 1
            stats.reasons[call.reason or "?"] += 1
        else:
            stats.ok += 1
        if call.duration is not None:
            stats.durations.append(call.duration)
        state.pulses.append(Pulse(ts, "result", call.group, call.success is not False))
        detail = call.reason
        if call.can_retry:
            detail += " · canRetry"
        if call.duration is not None:
            detail += f" · {call.duration:.1f}s"
        _log(state, ts, "body", f"{'✓' if call.success is not False else '✗'} {call.tool}", detail,
             "ok" if call.success is not False else "fail")
        if call.group == "move" and call.inventory_delta:
            _log(state, ts, "world", "⚠ 移动改了背包", _delta_text(call.inventory_delta), "warn")


def _delta_text(delta: dict[str, int]) -> str:
    return "  ".join(f"{item} {change:+d}" for item, change in sorted(delta.items()))


def _on_body(state: PanelState, record: dict[str, Any], ts: float) -> None:
    event = record["event"]
    if event == "body_state":
        frame: dict[str, Any] = {}
        pos = record.get("pos")
        if isinstance(pos, list) and len(pos) == 3:
            frame["pos"] = [round(float(value), 2) for value in pos]
        for key in ("health", "food", "oxygen", "selected_item", "selected_slot", "offhand_item", "dimension",
                    "yaw", "pitch", "hazard_unresolved", "pending_action_count"):
            if key in record:
                frame[key] = record[key]
        if isinstance(record.get("inventory_counts"), dict):
            frame["inventory_counts"] = record["inventory_counts"]
        if record.get("bot"):
            state.bot = state.bot or str(record["bot"])
        _merge_body(state, ts, frame)
    elif event == "body_progress":
        frame = {key: value for key, value in record.items()
                 if key not in ("seq", "ts", "session_id", "event", "tool_call_id", "action_id", "source", "lite")}
        _progress(state, ts, record.get("tool_call_id"), frame, bool(record.get("lite")))
    elif event == "direct_call_progress":
        sample = record.get("native_sample")
        if isinstance(sample, dict):
            _progress(state, ts, record.get("tool_call_id"), project_progress(sample), bool(sample.get("lite")))
    elif event == "world_edit":
        edit = {key: record.get(key) for key in ("pos", "before", "after", "op", "action_kind", "target", "index")}
        edit["ts"] = ts
        edit["tool_call_id"] = record.get("tool_call_id")
        call = state.call(record.get("tool_call_id"))
        edit["tool"] = call.tool if call else ""
        edit["during_move"] = bool(call and call.group == "move")
        state.world_edits.append(edit)
        if call is not None:
            call.edits.append(edit)
        state.pulses.append(Pulse(ts, "edit", ok=edit.get("target") is not False))
        verb = {"break": "拆", "place": "放"}.get(str(edit.get("op")), "改")
        flagged = edit.get("target") is False
        _log(state, ts, "world", f"{'⚠ ' if flagged else ''}{verb} {edit.get('before') if verb == '拆' else edit.get('after')}",
             f"{_pos_text(edit.get('pos'))} · {edit.get('action_kind') or ''}", "warn" if flagged else "")
    elif event == "body_events":
        for item in record.get("events") or ():
            if not isinstance(item, dict):
                continue
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            entry = {"ts": ts, "name": item.get("name"), "status": data.get("status"), "reason": data.get("reason"),
                     "position": data.get("position")}
            state.body_events.append(entry)
            _log(state, ts, "body", str(item.get("name") or "event"),
                 f"{data.get('reason') or ''} {_pos_text(data.get('position'))}",
                 "fail" if data.get("status") == "failed" else "")


def _pos_text(pos: Any) -> str:
    if isinstance(pos, (list, tuple)) and len(pos) == 3:
        return ",".join(str(int(value)) if float(value).is_integer() else f"{float(value):.1f}" for value in pos)
    return ""


def _on_people(state: PanelState, record: dict[str, Any], ts: float) -> None:
    event = record["event"]
    if event == "user_message":
        entry = {"ts": ts, "sender": record.get("sender") or "操作者", "content": str(record.get("content") or ""),
                 "command": record.get("command")}
        state.operator.append(entry)
        state.pulses.append(Pulse(ts, "operator"))
        _log(state, ts, "operator", str(record.get("command") or "指令"), _short(entry["content"], 70))
    elif event == "assistant_message":
        content = str(record.get("content") or "")
        state.speech.append((ts, content))
        _log(state, ts, "agent", "「说」", _short(content, 70))
    elif event in ("turn_profile", "turn_decision_context"):
        for key in ("model_route", "effort", "situational", "relationship", "tool_focus", "policy_tags",
                    "intent_kind", "lifecycle", "route_model", "decision_context", "has_durable_goal",
                    "context_frame"):
            if key in record and record[key] is not None:
                state.profile[key] = record[key]
    elif event == "session_command_body_interrupt":
        state.reflex.append({"ts": ts, "kind": "interrupt", "detail": record.get("command"), "facts": record})
        _log(state, ts, "operator", "中断", str(record.get("command") or ""), "warn")


def _on_reflex(state: PanelState, record: dict[str, Any], ts: float) -> None:
    event = record["event"]
    if event == "direct_supervision_request":
        state.supervision = {"ts": ts, "status": "asking", "tool_call_id": record.get("tool_call_id")}
    elif event == "direct_supervision_result":
        state.supervision = {"ts": ts, "status": "answered", "choice": record.get("choice"),
                             "elapsed_s": record.get("elapsed_s"), "tool_call_id": record.get("tool_call_id")}
        state.reflex.append({"ts": ts, "kind": "supervision", "detail": record.get("choice"), "facts": record})
        state.pulses.append(Pulse(ts, "reflex", ok=record.get("choice") != "unknown"))
        _log(state, ts, "reflex", "监督", f"{record.get('choice')} · {float(record.get('elapsed_s') or 0):.2f}s",
             "warn" if record.get("choice") in ("yield", "unknown") else "")
    elif event == "direct_supervision_finished":
        state.supervision = {**state.supervision, "finished_ts": ts, "reads": record.get("reads"),
                             "questions": record.get("questions")}
    elif event in ("direct_call_blocked", "direct_call_handoff"):
        kind = "blocked" if event == "direct_call_blocked" else "handoff"
        state.reflex.append({"ts": ts, "kind": kind, "detail": _short(record.get("facts"), 80), "facts": record})
        state.pulses.append(Pulse(ts, "reflex", ok=False))
        _log(state, ts, "reflex", "阻塞" if kind == "blocked" else "交接", _short(record.get("facts"), 70), "warn")
    elif event == "execution_cancelled":
        state.reflex.append({"ts": ts, "kind": "cancel", "detail": record.get("reason"), "facts": record})
        _log(state, ts, "reflex", "停止确认",
             f"{record.get('reason')} · idle={record.get('execution_idle')} ok={record.get('interrupt_ok')}")


HANDLERS: dict[str, Callable[[PanelState, dict[str, Any], float], None]] = {}
for _name in ("interactive_ready", "runtime_scope", "provider_manifest", "tool_manifest", "session_terminal",
              "task_outcome"):
    HANDLERS[_name] = _on_session
for _name in ("responses_prepare", "responses_request", "responses_context_counted", "responses_http_attempt",
              "responses_http_phase", "responses_http_headers", "responses_http_open", "responses_created",
              "responses_first_byte", "responses_first_activity", "responses_stream_activity",
              "responses_stream_summary", "responses_terminal", "responses_failed", "responses_eof_cleanup",
              "responses_compaction_started", "responses_compaction_finished", "responses_compaction_failed"):
    HANDLERS[_name] = _on_responses
HANDLERS["model_usage"] = _on_model_usage
for _name in ("responses_tool_started", "responses_tool_output", "tool_decision_context", "tool_invoke",
              "tool_result"):
    HANDLERS[_name] = _on_tool
for _name in ("body_state", "body_progress", "direct_call_progress", "world_edit", "body_events"):
    HANDLERS[_name] = _on_body
for _name in ("user_message", "assistant_message", "turn_profile", "turn_decision_context",
              "session_command_body_interrupt"):
    HANDLERS[_name] = _on_people
for _name in ("direct_supervision_request", "direct_supervision_result", "direct_supervision_finished",
              "direct_call_blocked", "direct_call_handoff", "execution_cancelled"):
    HANDLERS[_name] = _on_reflex
# Known records the panel shows only on the raw-event page.
IGNORED = frozenset({"tool_raw_result", "turn_completed", "session_turn_completed", "idle_body_state_poll_failed",
                     "idle_body_events_polled", "direct_supervision_unknown"})


def apply(state: PanelState, record: dict[str, Any]) -> PanelState:
    event = record.get("event")
    ts = _ts(record)
    state.events += 1
    if ts:
        state.first_ts = ts if state.first_ts is None else min(state.first_ts, ts)
        state.last_ts = ts if state.last_ts is None else max(state.last_ts, ts)
    if isinstance(record.get("seq"), int):
        state.last_seq = record["seq"]
    if not isinstance(event, str):
        state.unknown["?"] += 1
        return state
    state.event_counts[event] += 1
    handler = HANDLERS.get(event)
    if handler is None:
        if event not in IGNORED:
            state.unknown[event] += 1
        return state
    try:
        handler(state, record, ts)
    except Exception as error:  # A malformed record is counted, never fatal.
        state.unknown[f"{event}!{type(error).__name__}"] += 1
    return state


def fold(records: list[dict[str, Any]], state: PanelState | None = None) -> PanelState:
    state = state or PanelState()
    for record in records:
        apply(state, record)
    return state


# ----------------------------------------------------------------------------- derived views

@dataclass
class Alert:
    level: str
    text: str


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def alerts(state: PanelState, now: float, *, live: bool) -> list[Alert]:
    found: list[Alert] = []
    request = state.request
    if request is not None and request.status == "running":
        waited = now - request.phase_ts
        if waited >= 45:
            found.append(Alert("fail", f"模型请求停在「{PHASE_LABELS.get(request.phase, request.phase)}」{waited:.0f}s"))
        elif waited >= 15:
            found.append(Alert("warn", f"模型请求停在「{PHASE_LABELS.get(request.phase, request.phase)}」{waited:.0f}s"))
    for call in state.running_calls():
        stats = state.stats.get(call.tool)
        p95 = percentile(stats.durations, 0.95) if stats else None
        elapsed = now - (call.invoke_ts or now)
        if p95 and elapsed > max(5.0, 2 * p95):
            found.append(Alert("warn", f"{call.tool} 已运行 {elapsed:.0f}s（历史 p95 {p95:.1f}s）"))
    navigation = state.baritone.get("navigation") or {}
    if state.running_calls() and isinstance(navigation.get("idle_ms"), (int, float)) and navigation["idle_ms"] >= 3000:
        found.append(Alert("warn", f"导航无进展 {navigation['idle_ms'] / 1000:.1f}s（Body 报告）"))
    flagged = [edit for edit in state.world_edits if edit.get("target") is False and now - edit["ts"] < 120]
    if flagged:
        found.append(Alert("warn", f"移动改了世界 {len(flagged)} 处（近 2 分钟）"))
    if len(state.vitals) >= 2:
        latest, previous = state.vitals[-1], state.vitals[-2]
        if isinstance(latest[1], (int, float)) and isinstance(previous[1], (int, float)) \
                and latest[1] < previous[1] and now - latest[0] < 10:
            found.append(Alert("fail", f"受伤 {previous[1]:.0f} → {latest[1]:.0f}"))
    hostile = [entity for entity in state.entities if entity.get("hostile") and float(entity.get("dist") or 99) < 8]
    if hostile and state.body_ts and now - state.body_ts < 10:
        found.append(Alert("fail", f"敌对靠近 {hostile[0].get('type')} {hostile[0].get('dist')}m"))
    if live and state.terminal is None and state.last_ts and now - state.last_ts > 10:
        found.append(Alert("muted", f"trace 已 {now - state.last_ts:.0f}s 无新记录"))
    return found


PHASE_LABELS = {
    "prepare": "准备",
    "connect": "连接",
    "send": "发送",
    "wait": "等待首字节",
    "stream": "流式",
    "compact": "压缩",
    "done": "完成",
    "failed": "失败",
}


class Timeline:
    """Random access over a finished record list, via periodic state keyframes."""

    STRIDE = 200

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.keyframes: list[PanelState] = []
        state = PanelState()
        for index, record in enumerate(records):
            if index % self.STRIDE == 0:
                self.keyframes.append(copy.deepcopy(state))
            apply(state, record)
        self.final = state

    def state_at(self, count: int) -> PanelState:
        """State after the first ``count`` records."""
        count = max(0, min(count, len(self.records)))
        if count == len(self.records):
            return copy.deepcopy(self.final)
        base = min(count // self.STRIDE, len(self.keyframes) - 1) if self.keyframes else 0
        state = copy.deepcopy(self.keyframes[base]) if self.keyframes else PanelState()
        for record in self.records[base * self.STRIDE: count]:
            apply(state, record)
        return state


__all__ = [
    "Alert",
    "Call",
    "PHASE_LABELS",
    "PanelState",
    "Request",
    "Timeline",
    "alerts",
    "apply",
    "call_group",
    "fold",
    "percentile",
]
