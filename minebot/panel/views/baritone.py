"""Page 5: Baritone — processes, path, movement strip, path events and settings."""

from __future__ import annotations

from minebot.panel import theme
from minebot.panel.canvas import Canvas, pad
from minebot.panel.views.common import Frame, block_pos, draw_chrome, seconds, since
from minebot.panel.views.overview import PROCESS_LABELS, _event_label

MOVEMENT_STYLE = {
    "MovementTraverse": theme.layer("body"),
    "MovementAscend": theme.layer("agent"),
    "MovementDescend": theme.layer("operator"),
    "MovementFall": theme.layer("reflex"),
    "MovementDiagonal": theme.layer("client"),
    "MovementPillar": theme.layer("world"),
    "MovementParkour": theme.FAIL,
    "MovementDownward": theme.layer("baritone"),
}


def draw(canvas: Canvas, frame: Frame) -> None:
    _, top, width, height = draw_chrome(canvas, frame)
    state, now = frame.state, frame.now
    baritone = state.baritone
    navigation = baritone.get("navigation") or {}
    half = width // 2
    # processes
    canvas.box(1, top, half - 1, 6, theme.layer("baritone", dim=True), title=[("进程", theme.layer("baritone", bold=True))])
    processes = baritone.get("processes") or {}
    for index, (key, label) in enumerate(PROCESS_LABELS):
        on = bool(processes.get(key))
        cx = 3 + (index % 4) * ((half - 6) // 4)
        canvas.text(cx, top + 1 + index // 4, ("● " if on else "○ ") + label,
                    theme.layer("baritone", bold=True) if on else theme.DIM)
    canvas.runs(3, top + 3, [("goal ", theme.MUTED), (baritone.get("baritone_goal") or "—", theme.TEXT)], half - 6)
    canvas.runs(3, top + 4, [("pathing ", theme.MUTED), (str(baritone.get("pathing", "—")), theme.TEXT),
                             ("  paused ", theme.MUTED), (str(baritone.get("builder_paused", "—")), theme.TEXT),
                             ("  更新 ", theme.MUTED), (f"{seconds(now - state.baritone_ts)} 前" if state.baritone_ts else "—",
                                                       theme.TEXT)], half - 6)
    # path
    x = half + 1
    w = width - half - 2
    canvas.box(x, top, w, 6, theme.layer("baritone", dim=True), title=[("路径", theme.layer("baritone", bold=True))])
    position, total = navigation.get("path_position"), navigation.get("path_movements")
    if isinstance(position, int) and isinstance(total, int) and total:
        canvas.bar(x + 2, top + 1, w - 14, min(position + 1, total) / total, theme.layer("baritone"), theme.DIM)
        canvas.text(x + w - 11, top + 1, f"{min(position + 1, total)}/{total}", theme.BRIGHT)
    canvas.runs(x + 2, top + 2, [("src ", theme.MUTED), (block_pos(navigation.get("src")), theme.TEXT),
                                 ("  dest ", theme.MUTED), (block_pos(navigation.get("dest")), theme.TEXT),
                                 ("  ", None), (str(navigation.get("movement") or "—"), theme.layer("baritone", bold=True))], w - 4)
    idle = navigation.get("idle_ms")
    canvas.runs(x + 2, top + 3, [("idle ", theme.MUTED),
                                 (seconds(idle / 1000) if isinstance(idle, (int, float)) else "—",
                                  theme.WARN if isinstance(idle, (int, float)) and idle >= 3000 else theme.TEXT),
                                 ("  重复失败 ", theme.MUTED), (str(navigation.get("repeated_failures", "—")), theme.TEXT),
                                 ("  计算 ", theme.MUTED), (str(navigation.get("path_calculations", "—")), theme.TEXT),
                                 ("  计算中" if navigation.get("calculating") else "", theme.WARN)], w - 4)
    canvas.runs(x + 2, top + 4, [("脚下 ", theme.MUTED), (str(navigation.get("feet_block") or "—"), theme.TEXT),
                                 ("  上次失败 ", theme.MUTED), (str(navigation.get("last_failure") or "—"),
                                                              theme.FAIL if navigation.get("last_failure") else theme.DIM)], w - 4)
    # movement strip
    sy = top + 6
    canvas.box(1, sy, width - 2, 5, theme.layer("baritone", dim=True), title=[("动作序列 · 最近 60", theme.layer("baritone", bold=True))])
    movements = [entry for entry in state.movements if entry[0] <= now]
    for index, (_, movement, _src) in enumerate(movements[-(width - 6):]):
        canvas.put(3 + index, sy + 1, "█", MOVEMENT_STYLE.get(movement, theme.MUTED))
    used = 0
    for name, style in MOVEMENT_STYLE.items():
        used += canvas.text(3 + used, sy + 2, "█ " + name.removeprefix("Movement") + "  ", style)
    if movements:
        canvas.runs(3, sy + 3, [("当前 ", theme.MUTED), (movements[-1][1], theme.BRIGHT), ("  起点 ", theme.MUTED),
                                (block_pos(movements[-1][2]), theme.TEXT)])
    # pending + events + settings
    ry = sy + 5
    rh = height - (ry - top)
    third = (width - 2) // 3
    canvas.box(1, ry, third, rh, theme.layer("baritone", dim=True), title=[("待拆 · 待放", theme.layer("baritone", bold=True))])
    row = ry + 1
    for label, key, style in (("拆", "pending_break", theme.FAIL), ("放", "pending_place", theme.OK)):
        for cell in (navigation.get(key) or [])[: max(1, (rh - 2) // 2)]:
            canvas.runs(3, row, [(label + " ", style), (block_pos(cell) if isinstance(cell, list) else str(cell), theme.TEXT)], third - 4)
            row += 1
    if row == ry + 1:
        canvas.text(3, row, "无", theme.DIM)
    ex = third + 1
    canvas.box(ex, ry, third, rh, theme.layer("baritone", dim=True), title=[("寻路事件", theme.layer("baritone", bold=True))])
    events = baritone.get("path_events") or {}
    for index, (name, count) in enumerate(sorted(events.items(), key=lambda item: -item[1])[: (rh - 2) // 2]):
        canvas.runs(ex + 2, ry + 1 + index, [(pad(_event_label(name), 12), theme.MUTED), (str(count), theme.BRIGHT)])
    log = [entry for entry in state.path_event_log if entry[0] <= now][-((rh - 2) - (rh - 2) // 2):]
    base = ry + 1 + (rh - 2) // 2
    for index, (ts, name) in enumerate(log):
        canvas.runs(ex + 2, base + index, [(since(state, ts) + " ", theme.DIM), (_event_label(name), theme.TEXT)], third - 4)
    sx = 2 * third + 1
    sw = width - sx - 1
    canvas.box(sx, ry, sw, rh, theme.layer("baritone", dim=True), title=[("设置与建造", theme.layer("baritone", bold=True))])
    row = ry + 1
    for key, label in (("allow_break", "允许拆"), ("allow_place", "允许放"), ("allow_inventory", "允许整理背包")):
        if key in baritone:
            canvas.runs(sx + 2, row, [(pad(label, 14), theme.MUTED), ("是" if baritone[key] else "否", theme.TEXT)])
            row += 1
    construction = baritone.get("construction_progress") or {}
    if construction:
        matched = int(construction.get("matching_cells") or 0)
        expected = int(construction.get("expected_cells") or 0) or 1
        canvas.text(sx + 2, row + 1, f"建造 {construction.get('material_item') or ''}", theme.layer("world"))
        canvas.bar(sx + 2, row + 2, sw - 14, matched / expected, theme.layer("world"), theme.DIM)
        canvas.text(sx + sw - 11, row + 2, f"{matched}/{expected}", theme.BRIGHT)
