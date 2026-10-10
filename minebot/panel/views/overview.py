"""Page 1: the live architecture diagram."""

from __future__ import annotations

from rich.cells import cell_len

from minebot.panel import theme
from minebot.panel.canvas import BOX_DASHED, BOX_ROUND, Canvas, fit, pad, wrap
from minebot.panel.model import PHASE_LABELS, REQUEST_PHASES, Call, PanelState, percentile
from minebot.panel.views.common import (
    GROUP_LABELS,
    SOURCE_LABELS,
    Frame,
    block_pos,
    draw_chrome,
    log_rows,
    pos_text,
    seconds,
    short_block,
    since,
    spinner,
    wait_tone,
)

LAYOUT_WIDTH = 120
GROUPS = (("sense", 31, 27), ("move", 61, 28), ("work", 92, 27))
PROCESS_LABELS = (("goal", "CustomGoal"), ("builder", "Builder"), ("mine", "Mine"), ("get", "GetTo"),
                  ("explore", "Explore"), ("follow", "Follow"), ("farm", "Farm"), ("elytra", "Elytra"))
PRIORITY = (("stop", "停止"), ("l0", "本能"), ("l1", "威胁"), ("l2", "工作"), ("l3", "生活"))


def draw(canvas: Canvas, frame: Frame) -> None:
    _, top, width, height = draw_chrome(canvas, frame)
    if width < 110 or height < 37:
        draw_compact(canvas, frame, top, width, height)
        return
    ox = 0 if width >= 180 else (width - LAYOUT_WIDTH) // 2
    state, now = frame.state, frame.now
    wires = _wires(canvas, frame, ox)
    _operator_box(canvas, frame, ox + 1, 4, 28, 7)
    _reflex_box(canvas, frame, ox + 1, 11, 28, 8)
    _world_box(canvas, frame, ox + 1, 19, 28, 11)
    _agent_box(canvas, frame, ox + 31, 4, 88, 7)
    _catalog_box(canvas, frame, ox + 31, 13, 88, 3)
    for group, gx, gw in GROUPS:
        _group_box(canvas, frame, group, ox + gx, 18, gw, 5)
    _baritone_box(canvas, frame, ox + 50, 25, 69, 6)
    _body_box(canvas, frame, ox + 1, 32, 118, 4)
    _labels(canvas, frame, ox)
    _packets(canvas, frame, wires)
    if width >= 180:
        from minebot.panel.views.actions import draw_call_rows

        canvas.box(LAYOUT_WIDTH + 1, 4, width - LAYOUT_WIDTH - 2, 32, theme.layer("body", dim=True),
                   title=[("行动 · 最近调用", theme.layer("body", bold=True))])
        draw_call_rows(canvas, frame, LAYOUT_WIDTH + 3, 5, width - LAYOUT_WIDTH - 6, 30, compact=True)
    log_rows(canvas, frame, ox + 1 if width < 180 else 1, 36, (118 if width < 180 else width - 2), height + top - 37)
    del state, now


# ------------------------------------------------------------------ wires and packets

def _wires(canvas: Canvas, frame: Frame, ox: int) -> dict[str, list[tuple[int, int]]]:
    state, now = frame.state, frame.now
    traffic = {pulse.kind for pulse in state.pulses if 0 <= now - pulse.ts < 10}
    running = state.running_calls()
    active_groups = {call.group for call in running}
    recent_groups = {pulse.group for pulse in state.pulses if 0 <= now - pulse.ts < 10 and pulse.group}

    def style(name: str, hot: bool, dashed_layer: str | None = None) -> object:
        return theme.layer(dashed_layer or name, bold=hot) if hot else theme.layer(dashed_layer or name, dim=True)

    wires: dict[str, list[tuple[int, int]]] = {}
    wires["operator"] = canvas.wire([(ox + 29, 7), (ox + 30, 7)], style("operator", "operator" in traffic),
                                    dashed=True, arrow="▶")
    wires["reflex"] = canvas.wire([(ox + 29, 14), (ox + 30, 14)], style("reflex", "reflex" in traffic),
                                  dashed=True, arrow="▶")
    wires["agent"] = canvas.wire([(ox + 75, 11), (ox + 75, 12)], style("agent", bool(running) or "invoke" in traffic),
                                 arrow="▼")
    for group, gx, gw in GROUPS:
        cx = ox + gx + gw // 2
        hot = group in active_groups or group in recent_groups
        wires[f"group:{group}"] = canvas.wire([(cx, 16), (cx, 17)], style("body", hot), arrow="▼")
    sense_hot = "sense" in active_groups or "sense" in recent_groups
    wires["sense:body"] = canvas.wire([(ox + 44, 23), (ox + 44, 31)], style("body", sense_hot), arrow="▼")
    nav_hot = _baritone_live(frame)
    wires["move:baritone"] = canvas.wire([(ox + 75, 23), (ox + 75, 24)], style("client", nav_hot), arrow="▼")
    wires["work:baritone"] = canvas.wire([(ox + 105, 23), (ox + 105, 24)], style("client", nav_hot), arrow="▼")
    wires["baritone:body"] = canvas.wire([(ox + 84, 31), (ox + 84, 31)], style("baritone", nav_hot), arrow="▼")
    wires["edit"] = canvas.wire([(ox + 49, 27), (ox + 29, 27)], style("world", "edit" in traffic), dashed=True,
                                arrow="◀")
    canvas.put(ox + 44, 27, "┼", theme.layer("body", dim=True))
    return wires


def _baritone_live(frame: Frame) -> bool:
    state = frame.state
    if not state.running_calls() or state.baritone_ts is None or frame.now - state.baritone_ts > 2.0:
        return False
    processes = state.baritone.get("processes") or {}
    return bool(state.baritone.get("pathing") or any(processes.values()))


def _packets(canvas: Canvas, frame: Frame, wires: dict[str, list[tuple[int, int]]]) -> None:
    state, now, anim = frame.state, frame.now, frame.anim
    running = state.running_calls()
    for index, call in enumerate(running[-3:]):
        phase = (anim * 1.4 + index * 0.33) % 1.0
        canvas.dot(wires["agent"], phase, theme.layer("agent", bold=True), theme.layer("agent", dim=True), trail=1)
        group_wire = wires.get(f"group:{call.group}")
        if group_wire:
            canvas.dot(group_wire, (phase + 0.5) % 1.0, theme.layer("body", bold=True), theme.layer("body", dim=True),
                       trail=1)
    if _baritone_live(frame):
        bounce = abs((anim * 1.2) % 2.0 - 1.0)
        for name in ("move:baritone", "work:baritone"):
            group = name.split(":")[0]
            if any(call.group == group for call in running):
                canvas.dot(wires[name], bounce, theme.layer("baritone", bold=True), theme.layer("client"), trail=1)
        canvas.put(wires["baritone:body"][0][0], wires["baritone:body"][0][1],
                   "●" if int(anim * 4) % 2 else "▼", theme.layer("baritone", bold=True))
    for pulse in state.pulses:
        age = now - pulse.ts
        if age < 0:
            continue
        if pulse.kind == "result" and age < 1.2:
            path = list(reversed(wires.get(f"group:{pulse.group}", []))) + list(reversed(wires["agent"]))
            style = theme.OK if pulse.ok else theme.FAIL
            canvas.dot(path, age / 1.2, style, style, trail=2)
        elif pulse.kind == "edit" and age < 1.0:
            style = theme.layer("world", bold=True) if pulse.ok else theme.WARN
            canvas.dot(wires["edit"], age / 1.0, style, theme.layer("world"), trail=3)
        elif pulse.kind in ("operator", "reflex") and age < 1.0:
            canvas.dot(wires[pulse.kind], age / 1.0, theme.layer(pulse.kind, bold=True), theme.layer(pulse.kind), trail=2)
        elif pulse.kind == "emit" and age < 0.8:
            canvas.dot(wires["agent"], age / 0.8, theme.layer("agent", bold=True), theme.layer("agent"), trail=1)


def _labels(canvas: Canvas, frame: Frame, ox: int) -> None:
    state, now = frame.state, frame.now
    running = state.running_calls()
    if running:
        call = running[-1]
        canvas.runs(ox + 78, 11, [(call.tool, theme.layer("body", bold=True)), (" ", None),
                                  (call.args or "{}", theme.MUTED)], 40)
    frames = [ts for ts, _ in state.progress_frames if 0 <= now - ts <= 2.0]
    if frames:
        canvas.text(ox + 77, 23, f"IPC {len(frames) / 2:.0f} 帧/s", theme.layer("client"))
    elif state.event_counts["body_progress"] == 0 and state.event_counts["direct_call_progress"] == 0:
        canvas.text(ox + 77, 23, "IPC 进度帧 未接入", theme.DIM)


# ------------------------------------------------------------------ boxes

def _operator_box(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state = frame.state
    messages = [entry for entry in state.operator if entry["ts"] <= frame.now]
    hot = bool(messages) and frame.now - messages[-1]["ts"] < 3
    canvas.box(x, y, w, h, theme.layer("operator", bold=hot) if hot else theme.layer("operator", dim=True),
               title=[("操作者", theme.layer("operator", bold=True))])
    canvas.runs(x + 2, y + 1, [("指令  ", theme.MUTED), (str(len(messages)), theme.layer("operator", bold=True))])
    row = y + 2
    for entry in reversed(messages[-3:]):
        lines = wrap(entry["content"] or str(entry.get("command") or ""), w - 6, 2 if row + 1 < y + h - 1 else 1)
        for index, line in enumerate(lines):
            if row >= y + h - 1:
                break
            canvas.text(x + 2, row, ("» " if index == 0 else "  ") + line,
                        theme.layer("operator") if entry is messages[-1] else theme.MUTED, w - 4)
            row += 1


def _reflex_box(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now = frame.state, frame.now
    canvas.box(x, y, w, h, theme.layer("reflex", dim=True), title=[("反射 · 控制权", theme.layer("reflex", bold=True))])
    holding = any(call.mutating or call.group == "move" for call in state.running_calls())
    used = 0
    for index, (key, label) in enumerate(PRIORITY):
        style = theme.on("reflex") if key == "l2" and holding else theme.layer("reflex", dim=True)
        used += canvas.text(x + 2 + used, y + 1, label, style)
        if index + 1 < len(PRIORITY):
            used += canvas.text(x + 2 + used, y + 1, "›", theme.DIM)
    canvas.runs(x + 2, y + 2, [("持有 ", theme.MUTED),
                               ("agent_call" if holding else "—", theme.layer("reflex") if holding else theme.DIM),
                               ("  控制权事件 未接入", theme.DIM)], w - 4)
    supervision = state.supervision
    if supervision:
        if supervision.get("status") == "asking":
            canvas.runs(x + 2, y + 3, [("监督 ", theme.MUTED), (spinner(frame.anim) + " 询问中", theme.layer("reflex"))])
        else:
            choice = str(supervision.get("choice") or "—")
            tone = "warn" if choice in ("yield", "unknown") else "ok"
            canvas.runs(x + 2, y + 3, [("监督 ", theme.MUTED), (choice, theme.tone(tone, bold=True)),
                                       (f"  {seconds(supervision.get('elapsed_s'))}", theme.MUTED)])
    else:
        canvas.runs(x + 2, y + 3, [("监督 ", theme.MUTED), ("◇ 未触发", theme.DIM)])
    kinds = [entry["kind"] for entry in state.reflex if entry["ts"] <= now]
    canvas.runs(x + 2, y + 4, [("阻塞 ", theme.MUTED), (str(kinds.count("blocked")), theme.TEXT),
                               ("  交接 ", theme.MUTED), (str(kinds.count("handoff")), theme.TEXT),
                               ("  停止 ", theme.MUTED), (str(kinds.count("cancel") + kinds.count("interrupt")), theme.TEXT)])
    last = next((entry for entry in reversed(state.reflex) if entry["ts"] <= now), None)
    if last is not None:
        canvas.text(x + 2, y + 5, f"{since(state, last['ts'])} {last['kind']} {last.get('detail') or ''}",
                    theme.MUTED, w - 4)
    if state.outcome:
        status = str(state.outcome.get("status"))
        canvas.runs(x + 2, y + 6, [("结论 ", theme.MUTED),
                                   (status, theme.tone("ok" if status == "completed" else "warn", bold=True))], w - 4)


def _world_box(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now = frame.state, frame.now
    edits = [edit for edit in state.world_edits if edit["ts"] <= now]
    flagged = sum(1 for edit in edits if edit.get("target") is False)
    hot = bool(edits) and now - edits[-1]["ts"] < 3
    canvas.box(x, y, w, h, theme.layer("world", bold=hot) if hot else theme.layer("world", dim=True),
               title=[("世界改动", theme.layer("world", bold=True))])
    canvas.runs(x + 2, y + 1, [("放 ", theme.MUTED), (str(sum(1 for e in edits if e.get("op") == "place")), theme.OK),
                               ("  拆 ", theme.MUTED), (str(sum(1 for e in edits if e.get("op") == "break")), theme.FAIL),
                               ("  非目标 ", theme.MUTED), (str(flagged), theme.WARN if flagged else theme.TEXT)])
    if not edits:
        telemetry = state.event_counts["body_progress"] > 0
        canvas.text(x + 2, y + 3, "暂无方块改动" if telemetry else "未接入改动记录", theme.DIM, w - 4)
        if not telemetry:
            canvas.text(x + 2, y + 4, "（旧 trace 无此数据）", theme.DIM, w - 4)
        return
    for index, edit in enumerate(reversed(edits[-(h - 3):])):
        row = y + 2 + index
        op = edit.get("op")
        icon, style = {"break": ("✖", theme.FAIL), "place": ("▪", theme.OK)}.get(str(op), ("◆", theme.TEXT))
        block = short_block(edit.get("before") if op == "break" else edit.get("after"))
        flagged_edit = edit.get("target") is False
        canvas.text(x + 2, row, icon, style)
        canvas.text(x + 4, row, pad(block, 10), theme.WARN if flagged_edit else theme.TEXT)
        canvas.text(x + 15, row, fit(block_pos(edit.get("pos")), w - 18), theme.MUTED)
        if flagged_edit:
            canvas.put(x + w - 2, row, "⚠", theme.WARN)


def _agent_box(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now, anim = frame.state, frame.now, frame.anim
    request = state.request
    running_request = request is not None and request.status == "running" and request.started_ts <= now
    running_calls = state.running_calls()
    title = [("AGENT", theme.layer("agent", bold=True)), (" · ", theme.DIM),
             (str(state.profile.get("model_route") or state.model_route or "primary"), theme.layer("agent")),
             (" · ", theme.DIM), (str(state.profile.get("effort") or "—"), theme.layer("agent"))]
    canvas.box(x, y, w, h, theme.layer("agent", bold=running_request) if running_request else theme.layer("agent", dim=True),
               title=title)
    inner = w - 4
    if running_request:
        waited = now - request.phase_ts
        status = [(spinner(anim) + " ", theme.layer("agent", bold=True)),
                  (PHASE_LABELS.get(request.phase, request.phase), theme.layer("agent", bold=True)),
                  (f"  {seconds(now - request.started_ts)}", theme.tone(wait_tone(waited), bold=True))]
    elif running_calls:
        call = running_calls[-1]
        status = [("⧗ 等待工具 ", theme.MUTED), (call.tool, theme.layer("body", bold=True)),
                  (f"  {seconds(now - (call.invoke_ts or now))}", theme.TEXT)]
    else:
        idle_from = max([ts for ts in (state.last_ts,) if ts] or [now])
        status = [("◇ 空闲", theme.MUTED), (f"  {seconds(max(0.0, now - idle_from))}", theme.DIM)]
    canvas.runs(x + 2, y + 1, status, inner // 2)
    right = [("请求 ", theme.MUTED), (f"#{state.request_total}", theme.TEXT),
             ("   ctx ", theme.MUTED), (f"{state.context_tokens / 1000:.1f}k", theme.TEXT),
             ("   cache ", theme.MUTED), (f"{(state.cache_ratio or 0) * 100:.0f}%", theme.TEXT),
             ("   ", None), (state.model or "—", theme.layer("agent"))]
    width_right = sum(cell_len(text) for text, _ in right)
    canvas.runs(x + w - 2 - width_right, y + 1, right)
    # phase chain
    current = request.phase if running_request else None
    reached = {phase for phase, ts in (request.phases if request else []) if ts <= now} if running_request else set()
    used = 0
    for index, phase in enumerate(REQUEST_PHASES):
        label = PHASE_LABELS[phase]
        if phase == current:
            style = theme.on("agent")
        elif phase in reached:
            style = theme.layer("agent")
        else:
            style = theme.DIM
        used += canvas.text(x + 2 + used, y + 2, f" {label} ", style)
        if index + 1 < len(REQUEST_PHASES):
            used += canvas.text(x + 2 + used, y + 2, "▸", theme.DIM)
    if running_request and current == "stream":
        canvas.bar(x + 4 + used, y + 2, 12, (anim * 0.7) % 1.0, theme.layer("agent"), theme.DIM)
        used += 14
    if running_request:
        canvas.runs(x + 4 + used, y + 2, [("idle ", theme.MUTED), (seconds(request.byte_idle_s), theme.TEXT),
                                          ("  ", None), (f"{request.received_bytes // 1024}KB", theme.MUTED)])
    # speech, typed out once
    speech = [entry for entry in state.speech if entry[0] <= now]
    if speech:
        ts, content = speech[-1]
        shown = content[: max(1, int((now - ts) * 60))] if now - ts < 6 else content
        for index, line in enumerate(wrap("「" + shown + "」", inner, 2)):
            canvas.text(x + 2, y + 3 + index, line, theme.TEXT if now - ts < 30 else theme.MUTED, inner)
    profile = state.profile
    facts = []
    for key, label in (("situational", "情境"), ("relationship", "关系"), ("tool_focus", "焦点"), ("lifecycle", "生命周期")):
        value = profile.get(key)
        if value:
            facts.append(f"{label} {','.join(value) if isinstance(value, list) else value}")
    canvas.text(x + 2, y + 5, "  ·  ".join(facts), theme.DIM, inner)


def _catalog_box(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state = frame.state
    running = state.running_calls()
    active_sources = {call.source for call in running}
    total = sum(state.source_counts.values())
    canvas.box(x, y, w, h, theme.layer("body", bold=bool(running)) if running else theme.layer("body", dim=True),
               title=[("工具", theme.layer("body", bold=True)), (f" · {total} 次", theme.MUTED),
                      (f" · 目录 {state.tools_count}" if state.tools_count else "", theme.DIM)])
    used = 0
    for source, count in state.source_counts.most_common():
        label = SOURCE_LABELS.get(source, source)
        style = theme.on("body") if source in active_sources else theme.layer("body")
        used += canvas.text(x + 2 + used, y + 1, f" {label} ", style)
        used += canvas.text(x + 2 + used, y + 1, f"{count}   ", theme.BRIGHT)
        if used > w - 12:
            break


def _group_box(canvas: Canvas, frame: Frame, group: str, x: int, y: int, w: int, h: int) -> None:
    state, now, anim = frame.state, frame.now, frame.anim
    calls = [call for call in state.calls.values() if call.group == group and call.invoke_ts and call.invoke_ts <= now]
    running = [call for call in calls if call.result_ts is None or call.result_ts > now]
    finished = [call for call in calls if call.result_ts is not None and call.result_ts <= now]
    last = running[-1] if running else (max(finished, key=lambda call: call.result_ts or 0) if finished else None)
    flash = last is not None and not running and last.success is False and now - (last.result_ts or 0) < 0.3
    if flash or (last is not None and not running and last.success is False and now - (last.result_ts or 0) < 5):
        border = theme.FAIL
    elif running:
        border = theme.layer("body", bold=True)
    else:
        border = theme.layer("body", dim=True)
    canvas.box(x, y, w, h, border, title=[(GROUP_LABELS[group], theme.layer("body", bold=True))])
    inner = w - 4
    if last is None:
        canvas.text(x + 2, y + 2, "—", theme.DIM)
        return
    canvas.runs(x + 2, y + 1, [(last.tool, theme.BRIGHT if running else theme.TEXT), (" ", None),
                               (last.args, theme.MUTED)], inner)
    if running:
        elapsed = now - (last.invoke_ts or now)
        stats = state.stats.get(last.tool)
        p95 = percentile(stats.durations, 0.95) if stats else None
        tone = "warn" if p95 and elapsed > 2 * max(p95, 2.5) else "ok"
        canvas.runs(x + 2, y + 2, [(spinner(anim) + " ", theme.layer("body", bold=True)),
                                   (seconds(elapsed), theme.tone(tone, bold=True)),
                                   (f"   帧 {last.frames}" if last.frames else "", theme.MUTED)], inner)
    else:
        ok = last.success is not False
        canvas.runs(x + 2, y + 2, [("✓ " if ok else "✗ ", theme.OK if ok else theme.FAIL),
                                   (last.reason or "—", theme.TEXT if ok else theme.FAIL),
                                   (f"  {seconds(last.duration)}", theme.MUTED)], inner)
    failed = sum(1 for call in finished if call.success is False)
    canvas.runs(x + 2, y + 3, [("✓", theme.OK), (str(len(finished) - failed), theme.TEXT), (" ✗", theme.FAIL),
                               (str(failed), theme.TEXT),
                               (f"   改世界 {sum(len(call.edits) for call in calls)}" if group != "sense" else "",
                                theme.MUTED)], inner)


def _baritone_box(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now = frame.state, frame.now
    live = _baritone_live(frame)
    baritone = state.baritone
    freshness = "—"
    if state.baritone_ts:
        age = now - state.baritone_ts
        recent = [ts for ts, _ in state.progress_frames if 0 <= now - ts <= 2.0]
        freshness = f"{len(recent) / 2:.0f} 帧/s" if recent and state.running_calls() else f"{seconds(age)} 前"
    canvas.box(x, y, w, h, theme.layer("baritone", bold=live) if live else theme.layer("baritone", dim=True),
               title=[("BARITONE", theme.layer("baritone", bold=True)), (f" · {freshness}", theme.MUTED)])
    inner = w - 4
    if not baritone:
        canvas.text(x + 2, y + 2, "未观测（等待 Body 进度帧或导航结果）", theme.DIM, inner)
        return
    processes = baritone.get("processes") or {}
    used = 0
    for key, label in PROCESS_LABELS:
        on = bool(processes.get(key))
        used += canvas.text(x + 2 + used, y + 1, ("● " if on else "○ ") + label + "  ",
                            theme.layer("baritone", bold=True) if on else theme.DIM)
        if used > inner - 8:
            break
    navigation = baritone.get("navigation") or {}
    canvas.runs(x + 2, y + 2, [("goal ", theme.MUTED), (baritone.get("baritone_goal") or "—", theme.TEXT),
                               ("   ", None), (str(navigation.get("movement") or "—"), theme.layer("baritone"))], inner)
    position, total = navigation.get("path_position"), navigation.get("path_movements")
    used = canvas.text(x + 2, y + 3, "路径 ", theme.MUTED)
    if isinstance(position, int) and isinstance(total, int) and total > 0:
        canvas.bar(x + 2 + used, y + 3, 22, min(position + 1, total) / total, theme.layer("baritone"), theme.DIM)
        used += 23
        used += canvas.text(x + 2 + used, y + 3, f"{min(position + 1, total)}/{total}", theme.TEXT)
    else:
        used += canvas.text(x + 2 + used, y + 3, "—", theme.DIM)
    idle = navigation.get("idle_ms")
    if isinstance(idle, (int, float)):
        tone = "fail" if idle >= 6000 else "warn" if idle >= 3000 else "ok"
        used += canvas.runs(x + 4 + used, y + 3, [("idle ", theme.MUTED), (seconds(idle / 1000), theme.tone(tone, bold=True))])
    canvas.runs(x + 6 + used, y + 3, [("失败 ", theme.MUTED), (str(navigation.get("repeated_failures", 0)), theme.TEXT),
                                      ("  计算中" if navigation.get("calculating") else "", theme.WARN)])
    pending_break = navigation.get("pending_break") or []
    pending_place = navigation.get("pending_place") or []
    events = baritone.get("path_events") or {}
    event_text = " · ".join(f"{_event_label(name)} {count}" for name, count in list(events.items())[:4])
    canvas.runs(x + 2, y + 4, [("待拆 ", theme.MUTED), (_cells_text(pending_break), theme.FAIL if pending_break else theme.DIM),
                               ("  待放 ", theme.MUTED), (_cells_text(pending_place), theme.OK if pending_place else theme.DIM),
                               ("   ", None), (event_text, theme.MUTED)], inner)


def _event_label(name: str) -> str:
    return {"CALC_STARTED": "计算", "CANCELED": "取消", "CALC_FAILED": "计算失败", "AT_GOAL": "到达",
            "CALC_FINISHED_NOW_EXECUTING": "执行", "NEXT_SEGMENT_CALC_STARTED": "下一段",
            "NEXT_CALC_FAILED": "下一段失败", "PATH_FINISHED_NEXT_STILL_CALCULATING": "等下一段",
            "DISCARD_NEXT": "丢弃下一段", "CONTINUING_ONTO_PLANNED_NEXT": "接续"}.get(name, name)


def _cells_text(cells: list) -> str:
    if not cells:
        return "—"
    return " ".join(block_pos(cell) if isinstance(cell, list) else str(cell) for cell in cells[:2]) + (
        f" +{len(cells) - 2}" if len(cells) > 2 else "")


def _body_box(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now = frame.state, frame.now
    body = state.body
    fresh = state.body_ts is not None and now - state.body_ts < 3
    canvas.box(x, y, w, h, theme.layer("body", bold=fresh) if fresh else theme.layer("body", dim=True),
               title=[("身体", theme.layer("body", bold=True)),
                      (f" · {seconds(now - state.body_ts)} 前" if state.body_ts else " · 未观测", theme.MUTED)])
    if not body:
        return
    used = canvas.runs(x + 2, y + 1, [(pos_text(body.get("pos")), theme.BRIGHT), ("  ", None),
                                      (str(body.get("dimension") or "").removeprefix("minecraft:") or "—", theme.MUTED),
                                      ("  ", None), (_daytime(body.get("time")), theme.layer("world"))])
    used += 3
    for label, key, maximum, layer in (("HP ", "health", 20.0, "fail"), ("饥饿 ", "food", 20.0, "warn")):
        value = body.get(key)
        used += canvas.text(x + 2 + used, y + 1, label, theme.MUTED)
        if isinstance(value, (int, float)):
            canvas.bar(x + 2 + used, y + 1, 20, float(value) / maximum, theme.tone(layer), theme.DIM, "░")
            used += 21
            used += canvas.text(x + 2 + used, y + 1, f"{value:g}", theme.BRIGHT) + 3
        else:
            used += canvas.text(x + 2 + used, y + 1, "—   ", theme.DIM)
    oxygen = body.get("oxygen")
    if isinstance(oxygen, (int, float)) and oxygen < 300:
        canvas.runs(x + 2 + used, y + 1, [("氧气 ", theme.MUTED), (f"{oxygen / 15:.0f}/20", theme.WARN)])
    selected = body.get("selected_item") or "空手"
    count = state.inventory.get(str(selected), 0)
    parts = [("手持 ", theme.MUTED), (f"{selected}" + (f" ×{count}" if count else ""), theme.TEXT),
             ("   副手 ", theme.MUTED), (body.get("offhand_item") or "—", theme.TEXT)]
    effects = body.get("effects") or []
    if effects:
        parts += [("   效果 ", theme.MUTED), (",".join(effect.get("id", "") for effect in effects[:3]), theme.layer("reflex"))]
    near = state.entities[:1]
    if near:
        entity = near[0]
        parts += [("   附近 ", theme.MUTED),
                  (f"{entity.get('name') or entity.get('type')} {entity.get('dist')}m", theme.FAIL if entity.get("hostile") else theme.TEXT)]
    delta = _session_delta(state)
    if delta:
        parts += [("   背包Δ ", theme.MUTED), (delta, theme.layer("world"))]
    canvas.runs(x + 2, y + 2, parts, w - 4)


def _session_delta(state: PanelState) -> str:
    if state.inventory_baseline is None:
        return ""
    changes = []
    for item in set(state.inventory) | set(state.inventory_baseline):
        change = state.inventory.get(item, 0) - state.inventory_baseline.get(item, 0)
        if change:
            changes.append((abs(change), item, change))
    changes.sort(reverse=True)
    return "  ".join(f"{item} {change:+d}" for _, item, change in changes[:4])


def _daytime(ticks: object) -> str:
    if not isinstance(ticks, (int, float)):
        return ""
    ticks = int(ticks) % 24000
    hours = (ticks // 1000 + 6) % 24
    minutes = int(ticks % 1000 * 60 / 1000)
    icon = "☀" if 0 <= ticks < 12000 else "☾"
    return f"{icon} {hours:02d}:{minutes:02d}"


# ------------------------------------------------------------------ compact

def draw_compact(canvas: Canvas, frame: Frame, top: int, width: int, height: int) -> None:
    """Three stacked boxes for small terminals: Agent, Body call, Baritone; then the log."""
    box_width = width - 2
    _agent_box(canvas, frame, 1, top, box_width, 7)
    state, now = frame.state, frame.now
    running = state.running_calls()
    finished = state.finished_calls()
    call: Call | None = running[-1] if running else (finished[-1] if finished else None)
    canvas.box(1, top + 7, box_width, 3, theme.layer("body", bold=bool(running)) if running else theme.layer("body", dim=True),
               title=[("BODY · 工具", theme.layer("body", bold=True))])
    if call is not None:
        status = (spinner(frame.anim) + f" {seconds(now - (call.invoke_ts or now))}") if call.running else (
            ("✓ " if call.success is not False else "✗ ") + (call.reason or "") + f" {seconds(call.duration)}")
        canvas.runs(3, top + 8, [(call.tool + " ", theme.BRIGHT), (status, theme.TEXT), ("  ", None),
                                 (call.args, theme.MUTED)], box_width - 4)
    if height >= 18:
        _baritone_box(canvas, frame, 1, top + 10, box_width, 6)
        log_top = top + 16
    else:
        log_top = top + 10
    rows = top + height - log_top - 1
    if rows > 0:
        log_rows(canvas, frame, 1, log_top, box_width, rows)
