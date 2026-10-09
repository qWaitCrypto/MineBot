"""``minebot-panel``: the live terminal panel over a session trace.

The panel only ever reads trace.jsonl; it never talks to the Body, the model or IPC.
When a launcher hands it a ``controller``, operator lines (messages, /pause, /cancel)
go to the session through that controller, the same path as typing into the session.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Protocol

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Input

from minebot.panel import prefs
from minebot.panel.motion import Tweens
from minebot.panel.notices import Notifier
from minebot.panel.model import PanelState, Timeline, apply
from minebot.panel.source import TraceFollower, latest_trace, load_records, resolve_trace
from minebot.panel import theme
from minebot.panel.theme import BACKGROUND
from minebot.panel.views import PAGES, draw_help, draw_notice_history, draw_notices, draw_too_small, render
from minebot.panel.views import actions as actions_view
from minebot.panel.views import events as events_view
from minebot.panel.views.common import Frame

FPS = 15
POLL_S = 0.1
RAW_LIMIT = 20000
SPEEDS = (1, 4, 16, 64)
FLASH_S = 4.0
CANCEL_ARM_S = 1.5
HELP_HINTS = 3  # show the "? 快捷键" hint on the first few opens only
MIN_SIZE = (80, 24)


THEME_LABELS = {"dark": "深色", "light": "浅色", "contrast": "高对比", "mono": "无色"}


class Controller(Protocol):
    def send(self, line: str) -> bool: ...


class PanelView(Widget):
    """One full-screen canvas; the app decides what frame it shows."""

    def __init__(self, panel: "PanelApp") -> None:
        super().__init__()
        self.panel = panel

    def render(self) -> Any:
        width, height = self.size.width, self.size.height
        return self.panel.draw(width, height)


class PanelApp(App):
    AUTO_FOCUS = None  # the hidden command bar must not swallow panel keys
    CSS = f"""
    Screen {{ background: {BACKGROUND}; }}
    PanelView {{ width: 100%; height: 1fr; background: {BACKGROUND}; }}
    #command {{ dock: bottom; height: 3; display: none; background: {BACKGROUND};
               border: round {theme.LAYER["operator"]}; color: {theme.TONE["bright"]}; }}
    #command:focus {{ border: round {theme.LAYER["operator"]}; }}
    """
    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("right,tab", "page(1)", show=False),
        Binding("left,shift+tab", "page(-1)", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("pageup", "move(-10)", show=False),
        Binding("pagedown", "move(10)", show=False),
        Binding("enter", "detail", show=False),
        Binding("escape", "close", show=False),
        Binding("space", "pause", show=False),
        Binding("f", "follow", show=False),
        Binding("plus,equals_sign", "speed(1)", show=False),
        Binding("minus", "speed(-1)", show=False),
        Binding("right_square_bracket", "jump('failure', 1)", show=False),
        Binding("left_square_bracket", "jump('failure', -1)", show=False),
        Binding("u", "jump('operator', 1)", show=False),
        Binding("home", "seek_to(0)", show=False),
        Binding("end", "seek_to(1)", show=False),
        Binding("comma", "step(-50)", show=False),
        Binding("full_stop", "step(50)", show=False),
        Binding("slash", "filter", show=False),
        Binding("n", "newest", show=False),
        Binding("colon,t", "command", show=False),
        Binding("p", "toggle_pause", show=False),
        Binding("exclamation_mark", "cancel_action", show=False),
        Binding("question_mark", "help", show=False),
        Binding("N", "notices", show=False),
        Binding("T", "cycle_theme", show=False),
        *[Binding(str(index + 1), f"goto({index})", show=False) for index in range(len(PAGES))],
    ]

    def __init__(self, trace: Path | None, *, replay: bool, root: Path | None,
                 controller: Controller | None = None) -> None:
        super().__init__()
        self.trace = trace
        self.root = root
        self.page = 0
        self.selection: dict[str, int] = {}
        self.detail: Any = None
        self.detail_scroll = 0
        self.newer: Path | None = None
        self.controller = controller
        self.flash: tuple[str, str, float] | None = None  # (tone, text, until)
        self.history: list[str] = []
        self.history_index = 0
        self.agent_paused = False
        self._cancel_armed = 0.0
        self.show_help = False
        self.show_notices = False
        self.notifier = Notifier()
        self.connection: dict[str, Any] | None = None  # set by a launcher-backed shell
        self.tweens = Tweens()
        self.identity: dict[str, Any] | None = None  # set by a launcher-backed shell
        self.prefs = prefs.load()
        if self.prefs.get("theme") and not os.environ.get("NO_COLOR") and not os.environ.get("MINEBOT_THEME"):
            theme.set_theme(self.prefs["theme"])
        self.replay = replay
        self.follower = None
        self.timeline = None
        self.state = PanelState()
        self.raw: deque[dict[str, Any]] | list[dict[str, Any]] = deque(maxlen=RAW_LIMIT)
        self.title_text = ""
        self.playing = False
        self.speed = 1
        if trace is not None:
            self._load(trace, replay=replay)

    # ------------------------------------------------------------ sources
    def _load(self, trace: Path, *, replay: bool) -> None:
        self.trace = trace
        self.title_text = f"{trace.parent.parent.name}/{trace.parent.name}"
        records = load_records(trace)
        finished = any(record.get("event") == "session_terminal" for record in records)
        self.replay = replay or finished
        self.raw: deque[dict[str, Any]] | list[dict[str, Any]]
        if self.replay:
            self.timeline = Timeline(records)
            self.raw = records
            self.cursor = len(records)
            self.state = self.timeline.state_at(self.cursor)
            self.cursor_ts = float(records[-1].get("ts") or time.time()) if records else time.time()
            self.playing = False
            self.speed = 1
            self.follower = None
        else:
            self.timeline = None
            self.state = PanelState()
            self.raw = deque(maxlen=RAW_LIMIT)
            self.follower = TraceFollower(trace)
            self.notifier = Notifier()
            self._ingest()
            for notice in self.notifier.history:  # what happened before we opened is history, not news
                notice.shown_at = 0.0
            self.notifier.read_all()
            self.playing = True
            self.speed = 1

    def _ingest(self) -> None:
        if self.follower is None:
            return
        records, reset = self.follower.read()
        if reset:
            self.state = PanelState()
            self.raw.clear()
        for record in records:
            apply(self.state, record)
            self.raw.append(record)
            self.notifier.observe(record)

    def compose(self) -> ComposeResult:
        self.view = PanelView(self)
        yield self.view
        self.command = Input(placeholder="对机器人说话 · /goal 新目标 · /pause · /continue · /cancel   (enter 发送  esc 取消)",
                             id="command")
        yield self.command

    def on_mount(self) -> None:
        self._apply_background()
        page = int(self.prefs.get("page") or 0)
        self.page = page if 0 <= page < len(PAGES) else 0
        if self.prefs.get("speed") in SPEEDS:
            self.speed = self.prefs["speed"]
        hints = int(self.prefs.get("help_hints") or 0)
        if hints < HELP_HINTS:
            self.prefs["help_hints"] = hints + 1
            self._save_prefs()
            self.notify_flash("按 ? 看快捷键", "ok", 5.0)
        self.set_interval(1 / FPS, self._tick)
        self.set_interval(POLL_S, self._poll)
        if self.root is not None:
            self.set_interval(3.0, self._check_newer)
        self._last_tick = time.monotonic()

    def _poll(self) -> None:
        if not self.replay:
            self._ingest()

    def _tick(self) -> None:
        now = time.monotonic()
        elapsed, self._last_tick = now - self._last_tick, now
        if self.replay and self.playing and self.timeline is not None:
            self._advance(elapsed * self.speed)
        self.view.refresh()

    def _check_newer(self) -> None:
        self.run_worker(self._find_newer, thread=True, exclusive=True, group="newer")

    def _find_newer(self) -> None:
        # Directory scans can take a while on a large logs/ tree; keep them off the UI thread.
        found = latest_trace(self.root) if self.root else None
        self.newer = found if found is not None and found != self.trace else None

    # ------------------------------------------------------------ replay
    def _advance(self, seconds: float) -> None:
        records = self.timeline.records
        if self.cursor >= len(records):
            self.playing = False
            return
        self.cursor_ts += seconds
        next_ts = float(records[self.cursor].get("ts") or self.cursor_ts)
        if next_ts - self.cursor_ts > 5:  # skip idle gaps quickly
            self.cursor_ts = next_ts - 0.5
        while self.cursor < len(records) and float(records[self.cursor].get("ts") or 0) <= self.cursor_ts:
            apply(self.state, records[self.cursor])
            self.cursor += 1

    def _seek(self, count: int) -> None:
        if self.timeline is None:
            return
        records = self.timeline.records
        self.cursor = max(0, min(count, len(records)))
        self.state = self.timeline.state_at(self.cursor)
        if self.cursor:
            self.cursor_ts = float(records[self.cursor - 1].get("ts") or 0)
        elif records:
            self.cursor_ts = float(records[0].get("ts") or 0)

    # ------------------------------------------------------------ frame
    def frame(self) -> Frame:
        if self.replay:
            now, live = self.cursor_ts, False
            mode = "PAUSED" if not self.playing else f"{self.speed}×"
            replay = (self.cursor, len(self.timeline.records))
            raw = self.timeline.records[: self.cursor]
        else:
            live = True
            now = self.state.last_ts if self.state.terminal and self.state.last_ts else time.time()
            mode, replay, raw = "LIVE", None, list(self.raw)
        title = self.title_text
        if self.newer is not None:
            title = f"新实验 {self.newer.parent.name} · n 切换  |  {title}"
        flash = self.flash if self.flash is not None and self.flash[2] > time.monotonic() else None
        control = None
        if self.controller is not None and not self.replay:
            control = "已暂停" if self.agent_paused else "可控制"
        return Frame(state=self.state, now=now, anim=time.monotonic(), live=live, title=title, mode=mode,
                     page=self.page, replay=replay, selection=dict(self.selection), raw=raw,
                     control=control, flash=flash[:2] if flash else None,
                     connection=None if self.replay else self.connection,
                     tweens=None if self.replay else self.tweens,
                     identity=None if self.replay else self.identity)

    def draw(self, width: int, height: int) -> Any:
        frame = self.frame()
        self._frame = frame
        if width < MIN_SIZE[0] or height < MIN_SIZE[1]:
            return draw_too_small(frame, width, height, MIN_SIZE).to_text()
        canvas = render(frame, width, height, self.detail, self.detail_scroll)
        if self.show_notices:
            draw_notice_history(canvas, list(self.notifier.history))
        elif self.show_help:
            draw_help(canvas, controllable=frame.control is not None, replay=self.replay)
        elif not self.replay:
            draw_notices(canvas, self.notifier.visible(), self.notifier.unread)
        return canvas.to_text()

    # ------------------------------------------------------------ actions
    def action_goto(self, index: int) -> None:
        self.page = index
        self.detail = None
        self.prefs["page"] = index
        self._save_prefs()

    def action_page(self, step: int) -> None:
        self.action_goto((self.page + step) % len(PAGES))

    def _list_key(self) -> str | None:
        return {2: "actions", 6: "events"}.get(self.page)

    def _list_length(self) -> int:
        frame = self.frame()
        if self.page == 2:
            return len(actions_view.visible_calls(frame))
        if self.page == 6:
            return len(events_view.visible(frame))
        return 0

    def action_move(self, step: int) -> None:
        if self.command_open:
            self._history(step)
            return
        if self.detail is not None:
            self.detail_scroll = max(0, self.detail_scroll + step)
            return
        key = self._list_key()
        if key is None:
            return
        length = self._list_length()
        if not length:
            return
        current = self.selection.get(key, length)
        self.selection[key] = max(0, min(length - 1, current + step))

    def action_detail(self) -> None:
        frame = self.frame()
        if self.page == 2:
            calls = actions_view.visible_calls(frame)
            index = self.selection.get("actions", len(calls) - 1)
            if calls:
                call = calls[max(0, min(index, len(calls) - 1))]
                self.detail = {"invoke": call.invoke, "result": call.result, "world_edits": call.edits}
        elif self.page == 6:
            records = events_view.visible(frame)
            index = self.selection.get("events", len(records) - 1)
            if records:
                self.detail = records[max(0, min(index, len(records) - 1))]
        self.detail_scroll = 0

    def action_close(self) -> None:
        if self.command_open:
            self._close_command()
            return
        if self.show_help or self.show_notices:
            self.show_help = self.show_notices = False
            return
        if self.detail is not None:
            self.detail = None
        else:
            key = self._list_key()
            if key:
                self.selection.pop(key, None)

    def action_pause(self) -> None:
        if self.replay:
            if self.cursor >= len(self.timeline.records):
                self._seek(0)
            self.playing = not self.playing
            self._last_tick = time.monotonic()

    def action_follow(self) -> None:
        for key in ("actions", "events"):
            self.selection.pop(key, None)
        if self.replay:
            self._seek(len(self.timeline.records))
            self.playing = False

    def action_speed(self, step: int) -> None:
        index = SPEEDS.index(self.speed) if self.speed in SPEEDS else 0
        self.speed = SPEEDS[max(0, min(len(SPEEDS) - 1, index + step))]
        self.prefs["speed"] = self.speed
        self._save_prefs()

    # ------------------------------------------------------------ presentation
    def action_help(self) -> None:
        if not self.command_open:
            self.show_help, self.show_notices = not self.show_help, False

    def action_notices(self) -> None:
        if not self.command_open:
            self.show_notices, self.show_help = not self.show_notices, False
            self.notifier.read_all()

    def action_cycle_theme(self) -> None:
        if self.command_open:
            return
        names = theme.THEMES
        name = theme.set_theme(names[(names.index(theme.active_theme()) + 1) % len(names)])
        self._apply_background()
        self.prefs["theme"] = name
        self._save_prefs()
        self.notify_flash(f"主题：{THEME_LABELS.get(name, name)}", "ok", 2.0)

    def _apply_background(self) -> None:
        color = theme.background()
        try:
            self.screen.styles.background = color
            self.view.styles.background = color
            self.command.styles.background = color
        except Exception:  # not mounted yet
            pass

    def _save_prefs(self) -> None:
        prefs.save(self.prefs)

    def action_step(self, count: int) -> None:
        if self.replay:
            self._seek(self.cursor + count)

    def action_seek_to(self, end: int) -> None:
        if self.replay:
            self._seek(len(self.timeline.records) if end else 0)

    def action_jump(self, kind: str, direction: int) -> None:
        if not self.replay:
            return
        records = self.timeline.records

        def hit(record: dict[str, Any]) -> bool:
            if kind == "failure":
                full = record.get("full_result") if isinstance(record.get("full_result"), dict) else {}
                return record.get("event") == "tool_result" and full.get("success", record.get("success")) is False \
                    or record.get("event") in ("responses_failed", "direct_call_blocked")
            return record.get("event") == "user_message"

        indices = range(self.cursor, len(records)) if direction > 0 else range(self.cursor - 2, -1, -1)
        for index in indices:
            if hit(records[index]):
                self._seek(index + 1)
                self.playing = False
                return

    def action_filter(self) -> None:
        if self.page == 6:
            self.selection["filter"] = (self.selection.get("filter", 0) + 1) % len(events_view.FILTERS)
            self.selection.pop("events", None)

    def action_newest(self) -> None:
        if self.newer is not None:
            self.selection.clear()
            self.detail = None
            self._load(self.newer, replay=False)
            self.newer = None

    # ------------------------------------------------------------ control
    @property
    def command_open(self) -> bool:
        command = getattr(self, "command", None)
        return command is not None and bool(command.display)

    def notify_flash(self, text: str, tone: str = "ok", duration: float = FLASH_S) -> None:
        self.flash = (tone, text, time.monotonic() + duration)

    def _can_control(self) -> bool:
        if self.controller is None:
            self.notify_flash("只读面板：用 minebot 启动的会话才能在这里控制", "warn")
            return False
        if self.replay:
            self.notify_flash("回放中，不能控制", "warn")
            return False
        return True

    def send_line(self, line: str) -> bool:
        if not self._can_control():
            return False
        ok = self.controller.send(line)
        if not ok:
            self.notify_flash("发送失败：Agent 会话已退出", "fail")
        return ok

    def action_command(self) -> None:
        if not self._can_control():
            return
        self.history_index = len(self.history)
        self.command.value = ""
        self.command.display = True
        self.command.focus()

    def _close_command(self) -> None:
        self.command.display = False
        self.command.value = ""
        self.set_focus(None)

    def _history(self, step: int) -> None:
        if not self.history:
            return
        self.history_index = max(0, min(len(self.history), self.history_index + step))
        self.command.value = self.history[self.history_index] if self.history_index < len(self.history) else ""
        self.command.cursor_position = len(self.command.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        self._close_command()
        if not line:
            return
        if not self.history or self.history[-1] != line:
            self.history.append(line)
        if self.send_line(line):
            lowered = line.lower()
            if lowered.startswith("/pause"):
                self.agent_paused = True
            elif lowered.startswith("/continue"):
                self.agent_paused = False
            self.notify_flash(f"已发送 {line[:60]}", "ok")

    def action_toggle_pause(self) -> None:
        if self.command_open:
            return
        command = "/continue" if self.agent_paused else "/pause"
        if self.send_line(command):
            self.agent_paused = not self.agent_paused
            self.notify_flash("已暂停 Agent：不再发起新的请求和动作 · p 继续" if self.agent_paused else "已继续", "warn" if self.agent_paused else "ok")

    def action_cancel_action(self) -> None:
        if self.command_open or not self._can_control():
            return
        now = time.monotonic()
        if now - self._cancel_armed > CANCEL_ARM_S:
            self._cancel_armed = now
            self.notify_flash("再按一次 ! 取消当前动作", "warn", CANCEL_ARM_S)
            return
        self._cancel_armed = 0.0
        if self.send_line("/cancel panel_cancel"):
            self.notify_flash("已取消当前动作", "fail")


# ------------------------------------------------------------------ CLI

def default_root() -> Path:
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "logs").is_dir() and (candidate / "minebot").is_dir():
            return candidate / "logs"
    return here / "logs"


def tmux_layout(trace: Path, argv: list[str]) -> int:
    """Panel on the left, agent.log and client.log tails on the right."""
    if shutil.which("tmux") is None:
        print("tmux not found", file=sys.stderr)
        return 2
    run_dir = trace.parent
    panel = shlex.join([sys.executable, "-m", "minebot.panel.app", *[arg for arg in argv if arg != "--tmux"]])
    tails = [f"tail -n 200 -F {shlex.quote(str(run_dir / name))}" for name in ("agent.log", "client.log")]
    if os.environ.get("TMUX"):
        subprocess.run(["tmux", "split-window", "-h", "-l", "40%", tails[0]], check=True)
        subprocess.run(["tmux", "split-window", "-v", tails[1]], check=True)
        subprocess.run(["tmux", "select-pane", "-L"], check=True)
        os.execvp(sys.executable, [sys.executable, "-m", "minebot.panel.app",
                                   *[arg for arg in argv if arg != "--tmux"]])
    session = "minebot-panel"
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "240", "-y", "60", panel], check=True)
    subprocess.run(["tmux", "split-window", "-h", "-l", "40%", "-t", session, tails[0]], check=True)
    subprocess.run(["tmux", "split-window", "-v", "-t", session, tails[1]], check=True)
    subprocess.run(["tmux", "select-pane", "-t", f"{session}:0.0"], check=True)
    os.execvp("tmux", ["tmux", "attach", "-t", session])
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="minebot-panel", description="MineBot live terminal panel (read-only).")
    parser.add_argument("target", nargs="?", help="run directory or trace.jsonl; default: newest trace under logs/")
    parser.add_argument("--replay", action="store_true", help="replay even if the session is still running")
    parser.add_argument("--root", type=Path, help="directory searched for the newest trace (default: ./logs)")
    parser.add_argument("--tmux", action="store_true", help="open panel + agent.log + client.log in tmux")
    args = parser.parse_args(argv)
    root = args.root or default_root()
    try:
        trace = resolve_trace(args.target) if args.target else latest_trace(root)
    except FileNotFoundError as error:
        print(f"minebot-panel: {error}", file=sys.stderr)
        return 2
    if trace is None:
        print(f"minebot-panel: no trace.jsonl under {root}", file=sys.stderr)
        return 2
    if args.tmux:
        return tmux_layout(trace, argv)
    PanelApp(trace, replay=args.replay, root=None if args.target else root).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Controller", "PanelApp", "main"]
