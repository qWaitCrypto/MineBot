"""``minebot-panel``: the live terminal panel over a session trace.

Read-only: it tails trace.jsonl and never talks to the Body, the model or IPC.
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
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget

from minebot.panel.model import PanelState, Timeline, apply
from minebot.panel.source import TraceFollower, latest_trace, load_records, resolve_trace
from minebot.panel.theme import BACKGROUND
from minebot.panel.views import PAGES, render
from minebot.panel.views import actions as actions_view
from minebot.panel.views import events as events_view
from minebot.panel.views.common import Frame

FPS = 15
POLL_S = 0.1
RAW_LIMIT = 20000
SPEEDS = (1, 4, 16, 64)


class PanelView(Widget):
    """One full-screen canvas; the app decides what frame it shows."""

    def __init__(self, panel: "PanelApp") -> None:
        super().__init__()
        self.panel = panel

    def render(self) -> Any:
        width, height = self.size.width, self.size.height
        return self.panel.draw(width, height)


class PanelApp(App):
    CSS = f"""
    Screen {{ background: {BACKGROUND}; }}
    PanelView {{ width: 100%; height: 100%; background: {BACKGROUND}; }}
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
        *[Binding(str(index + 1), f"goto({index})", show=False) for index in range(len(PAGES))],
    ]

    def __init__(self, trace: Path, *, replay: bool, root: Path | None) -> None:
        super().__init__()
        self.trace = trace
        self.root = root
        self.page = 0
        self.selection: dict[str, int] = {}
        self.detail: Any = None
        self.detail_scroll = 0
        self.newer: Path | None = None
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
            self._ingest()
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

    def compose(self) -> ComposeResult:
        self.view = PanelView(self)
        yield self.view

    def on_mount(self) -> None:
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
        return Frame(state=self.state, now=now, anim=time.monotonic(), live=live, title=title, mode=mode,
                     page=self.page, replay=replay, selection=dict(self.selection), raw=raw)

    def draw(self, width: int, height: int) -> Any:
        frame = self.frame()
        self._frame = frame
        canvas = render(frame, width, height, self.detail, self.detail_scroll)
        return canvas.to_text()

    # ------------------------------------------------------------ actions
    def action_goto(self, index: int) -> None:
        self.page = index
        self.detail = None

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


__all__ = ["PanelApp", "main"]
