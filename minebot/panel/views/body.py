"""Page 4: vitals, hotbar and inventory, a braille minimap and the world-edit timeline."""

from __future__ import annotations

import math

from minebot.panel import theme
from minebot.panel.canvas import Braille, Canvas, fit, pad
from minebot.panel.views.common import Frame, block_pos, draw_chrome, pos_text, seconds, short_block, since


def draw(canvas: Canvas, frame: Frame) -> None:
    _, top, width, height = draw_chrome(canvas, frame)
    left = min(62, width // 2)
    _vitals(canvas, frame, 1, top, left, 8)
    _hotbar(canvas, frame, 1, top + 8, left, 5)
    _inventory(canvas, frame, 1, top + 13, left, height - 13)
    map_width = width - left - 3
    map_height = min(height - 8, max(12, (map_width - 4) // 2 + 2))
    _minimap(canvas, frame, left + 2, top, map_width, map_height)
    _edits(canvas, frame, left + 2, top + map_height, map_width, height - map_height)


def _vitals(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now = frame.state, frame.now
    body = state.body
    canvas.box(x, y, w, h, theme.layer("body", dim=True), title=[("生命体征", theme.layer("body", bold=True)),
               (f" · {seconds(now - state.body_ts)} 前" if state.body_ts else "", theme.MUTED)])
    vitals = [entry for entry in state.vitals if entry[0] <= now]
    for index, (label, key, maximum, style, column) in enumerate((
            ("HP  ", "health", 20.0, theme.tone("fail"), 1), ("饥饿", "food", 20.0, theme.tone("warn"), 2),
            ("氧气", "oxygen", 300.0, theme.layer("body"), 3))):
        row = y + 1 + index
        value = body.get(key)
        used = canvas.text(x + 2, row, label + " ", theme.MUTED)
        if isinstance(value, (int, float)):
            canvas.bar(x + 2 + used, row, 20, float(value) / maximum, style, theme.DIM)
            used += 21
            used += canvas.text(x + 2 + used, row, pad(f"{value:g}", 5), theme.BRIGHT)
            history = [entry[column] for entry in vitals[-(w - used - 6):] if entry[column] is not None]
            if len(set(history)) > 1:
                canvas.sparkline(x + 2 + used, row, w - used - 5, history, style, min(history), max(history))
            else:
                canvas.text(x + 2 + used, row, "稳定", theme.DIM)
        else:
            canvas.text(x + 2 + used, row, "—", theme.DIM)
    canvas.runs(x + 2, y + 4, [("坐标 ", theme.MUTED), (pos_text(body.get("pos"), 2), theme.BRIGHT),
                               ("  朝向 ", theme.MUTED), (f"{body.get('yaw', 0):.0f}° / {body.get('pitch', 0):.0f}°"
                                                         if isinstance(body.get("yaw"), (int, float)) else "—", theme.TEXT)], w - 4)
    canvas.runs(x + 2, y + 5, [("维度 ", theme.MUTED), (str(body.get("dimension") or "—").removeprefix("minecraft:"), theme.TEXT),
                               ("  天气 ", theme.MUTED), (str(body.get("weather") or "—"), theme.TEXT),
                               ("  着地 ", theme.MUTED), ("是" if body.get("on_ground") else "否" if "on_ground" in body else "—", theme.TEXT)],
                w - 4)
    effects = body.get("effects") or []
    hazard = body.get("hazard_unresolved")
    canvas.runs(x + 2, y + 6, [("效果 ", theme.MUTED),
                               (", ".join(f"{effect.get('id')}" for effect in effects) or "—", theme.layer("reflex")),
                               ("  危险 ", theme.MUTED), (str(hazard) if hazard else "—", theme.FAIL if hazard else theme.DIM)],
                w - 4)


def _hotbar(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state = frame.state
    canvas.box(x, y, w, h, theme.layer("body", dim=True), title=[("快捷栏", theme.layer("body", bold=True))])
    hotbar = state.hotbar
    selected = state.body.get("selected_slot")
    if hotbar is None:
        canvas.text(x + 2, y + 1, "未观测（需 Body 完整进度帧）", theme.DIM, w - 4)
        canvas.runs(x + 2, y + 2, [("手持 ", theme.MUTED), (str(state.body.get("selected_item") or "—"), theme.TEXT)])
        return
    cell = max(5, (w - 4) // 9)
    for index, slot in enumerate(hotbar[:9]):
        cx = x + 2 + index * cell
        chosen = index == selected
        style = theme.on("body") if chosen else theme.TEXT
        name = slot[0] if slot else ""
        count = slot[1] if slot else 0
        canvas.text(cx, y + 1, pad(f"{index + 1}", cell - 1), theme.layer("body", bold=True) if chosen else theme.DIM)
        canvas.text(cx, y + 2, pad(_abbrev(name), cell - 1), style if slot else theme.DIM)
        canvas.text(cx, y + 3, pad(f"×{count}" if slot else "·", cell - 1), theme.MUTED)


def _abbrev(name: str) -> str:
    parts = name.split("_")
    if len(name) <= 6:
        return name
    return "".join(part[:3] if index == 0 else part[:1] for index, part in enumerate(parts))[:6]


def _inventory(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state = frame.state
    canvas.box(x, y, w, h, theme.layer("world", dim=True), title=[("背包 · 本次会话变化", theme.layer("world", bold=True))])
    baseline = state.inventory_baseline or {}
    items = sorted(set(state.inventory) | set(baseline), key=lambda item: (-(state.inventory.get(item, 0)), item))
    columns = 2 if w >= 50 else 1
    column_width = (w - 4) // columns
    rows = h - 2
    for index, item in enumerate(items[: rows * columns]):
        cx = x + 2 + (index // rows) * column_width
        row = y + 1 + index % rows
        count = state.inventory.get(item, 0)
        change = count - baseline.get(item, 0)
        canvas.text(cx, row, pad(item, column_width - 12), theme.TEXT if count else theme.DIM)
        canvas.text(cx + column_width - 12, row, pad(str(count), 5), theme.BRIGHT)
        if change:
            canvas.text(cx + column_width - 7, row, f"{change:+d}", theme.OK if change > 0 else theme.FAIL)


def _minimap(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now = frame.state, frame.now
    canvas.box(x, y, w, h, theme.layer("world", dim=True), title=[("小地图 · 俯视", theme.layer("world", bold=True)),
               ("  · 轨迹  ● 自己  ◆ 路径终点  红 敌对  绿 放  红 拆", theme.MUTED)])
    pos = state.body.get("pos")
    if not (isinstance(pos, list) and len(pos) == 3):
        canvas.text(x + 2, y + 2, "未观测", theme.DIM)
        return
    grid = Braille(w - 2, h - 2)
    cx, cy, cz = (float(value) for value in pos)
    reach = [abs(point[axis] - origin) for _, point in list(state.trail)[-300:]
             for axis, origin in ((0, cx), (2, cz))]
    reach += [abs(entity["pos"][axis] - origin) for entity in state.entities if isinstance(entity.get("pos"), list)
              for axis, origin in ((0, cx), (2, cz))]
    span = float(min(64, max(8, math.ceil(max(reach or [0]) * 1.25))))
    scale_x = grid.pixel_width / (2 * span)
    scale_z = grid.pixel_height / (2 * span)

    def pixel(wx: float, wz: float) -> tuple[int, int]:
        return int((wx - cx + span) * scale_x), int((wz - cz + span) * scale_z)

    trail = [entry for entry in state.trail if entry[0] <= now]
    for index, (_, point) in enumerate(trail):
        px, pz = pixel(point[0], point[2])
        age = len(trail) - index
        grid.set(px, pz, theme.layer("body", dim=age > 60), 1)
    navigation = state.baritone.get("navigation") or {}
    for edit in state.world_edits:
        if edit["ts"] > now or not isinstance(edit.get("pos"), list):
            continue
        px, pz = pixel(edit["pos"][0] + 0.5, edit["pos"][2] + 0.5)
        style = theme.FAIL if edit.get("op") == "break" else theme.OK
        grid.set(px, pz, style, 3)
    for entity in state.entities:
        epos = entity.get("pos")
        if isinstance(epos, list) and len(epos) == 3:
            px, pz = pixel(epos[0], epos[2])
            style = theme.FAIL if entity.get("hostile") else theme.layer("operator") if entity.get("type") == "player" else theme.MUTED
            for dx in (0, 1):
                grid.set(px + dx, pz, style, 4)
    grid.blit(canvas, x + 1, y + 1)
    dest = navigation.get("dest")
    if isinstance(dest, list) and len(dest) == 3:
        px, pz = pixel(dest[0] + 0.5, dest[2] + 0.5)
        canvas.put(x + 1 + px // 2, y + 1 + pz // 4, "◆", theme.layer("baritone", bold=True))
    me_x, me_z = pixel(cx, cz)
    canvas.put(x + 1 + me_x // 2, y + 1 + me_z // 4, "●", theme.BRIGHT)
    heading = state.body.get("yaw")
    if isinstance(heading, (int, float)):
        radians = math.radians(float(heading))
        hx, hz = pixel(cx - math.sin(radians) * 4, cz + math.cos(radians) * 4)
        canvas.put(x + 1 + hx // 2, y + 1 + hz // 4, "·", theme.BRIGHT)
    for index, entity in enumerate(state.entities[:3]):
        epos = entity.get("pos")
        if not (isinstance(epos, list) and len(epos) == 3):
            continue
        dy = epos[1] - cy
        marker = "▲" if dy > 2 else "▼" if dy < -2 else ""
        canvas.runs(x + 2, y + h - 2 - index, [(f"{entity.get('name') or entity.get('type')} ",
                                                 theme.FAIL if entity.get("hostile") else theme.TEXT),
                                                (f"{entity.get('dist')}m {marker}", theme.MUTED)], w - 4)


def _edits(canvas: Canvas, frame: Frame, x: int, y: int, w: int, h: int) -> None:
    state, now = frame.state, frame.now
    edits = [edit for edit in state.world_edits if edit["ts"] <= now]
    canvas.box(x, y, w, h, theme.layer("world", dim=True), title=[("世界改动时间线", theme.layer("world", bold=True)),
               (f" · {len(edits)}", theme.MUTED)])
    if not edits:
        telemetry = state.event_counts["body_progress"] > 0
        canvas.text(x + 2, y + 1, "暂无方块改动" if telemetry else "该 trace 没有改动记录（面板遥测之前的实验）", theme.DIM, w - 4)
        return
    for index, edit in enumerate(reversed(edits[-(h - 2):])):
        row = y + 1 + index
        flagged = edit.get("target") is False
        op = {"break": "拆", "place": "放"}.get(str(edit.get("op")), "改")
        used = canvas.text(x + 2, row, since(state, edit["ts"]) + " ", theme.MUTED)
        used += canvas.text(x + 2 + used, row, op + " ", theme.FAIL if op == "拆" else theme.OK)
        used += canvas.text(x + 2 + used, row, pad(f"{short_block(edit.get('before'))}→{short_block(edit.get('after'))}", 28),
                            theme.WARN if flagged else theme.TEXT)
        used += canvas.text(x + 2 + used, row, pad(block_pos(edit.get("pos")), 14), theme.MUTED)
        target = edit.get("target")
        used += canvas.text(x + 2 + used, row, pad("目标" if target else "非目标" if target is False else "未声明", 7),
                            theme.WARN if flagged else theme.MUTED)
        canvas.text(x + 2 + used, row, fit(f"{edit.get('tool') or ''} {edit.get('action_kind') or ''}", w - used - 4),
                    theme.DIM)
