"""``minebot``: one command from a cold start to the bot in the world and its panel on screen.

The launch screen walks environment → account → server → client → world → Agent, then the
same app turns into the live panel over the new session's trace. Quitting stops the Agent
gracefully and closes the game client this command started.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from textual.binding import Binding

from minebot.panel import theme
from minebot.panel.app import PanelApp
from minebot.panel.canvas import BOX_HEAVY, Canvas
from minebot.panel.views import launch as launch_view
from minebot.shell.launch import LaunchOptions, Launcher, tail_lines

READY_HOLD_S = 0.6
LOG_FILES = (("agent.log", "agent_log"), ("client.log", "client_log"))


class ShellApp(PanelApp):
    BINDINGS = [
        Binding("r", "retry", show=False),
        Binding("l", "log", show=False),
        Binding("k", "keep_client", show=False),
    ]

    def __init__(self, launcher: Launcher) -> None:
        super().__init__(None, replay=False, root=None, controller=launcher)
        self.launcher = launcher
        self.stage = "launch"  # launch | panel | stopping
        self.confirm_quit = False
        self.log_index: int | None = None
        self.log_scroll = 0
        self._ready_at: float | None = None
        self._exit_notified = False

    # ------------------------------------------------------------ lifecycle
    def on_mount(self) -> None:
        super().on_mount()
        self._start_launch()

    def _start_launch(self) -> None:
        self.run_worker(self.launcher.run, thread=True, exclusive=True, group="launch")

    def _tick(self) -> None:
        if self.stage == "launch" and self.launcher.phase == "ready":
            if self._ready_at is None:
                self._ready_at = time.monotonic()
            elif time.monotonic() - self._ready_at >= READY_HOLD_S:
                self._load(self.launcher.trace_path, replay=False)
                self.title_text = f"{self.launcher.bot}@{self.launcher.server}"
                self.stage = "panel"
                self.log_index = None
                self.notify_flash(": 对机器人说话 · p 暂停 · !! 取消动作 · q 退出", "ok", 6.0)
        if self.stage == "panel" and not self._exit_notified and self.launcher.session is not None \
                and self.launcher.session.poll() is not None:
            self._exit_notified = True
            self.notify_flash(f"Agent 会话已退出（代码 {self.launcher.session.returncode}）· q 退出", "fail", 3600)
        if self.stage == "stopping" and self.launcher.phase == "stopped":
            self.exit()
        super()._tick()

    # ------------------------------------------------------------ drawing
    def draw(self, width: int, height: int) -> Any:
        if self.stage == "panel" and self.log_index is None:
            text = super().draw(width, height)
            if not self.confirm_quit:
                return text
            canvas = self._panel_canvas(width, height)
            self._draw_confirm(canvas)
            return canvas.to_text()
        canvas = Canvas(width, height)
        launch_view.draw(canvas, self.launcher, time.monotonic(), log=self._log_view())
        if self.confirm_quit:
            self._draw_confirm(canvas)
        return canvas.to_text()

    def _panel_canvas(self, width: int, height: int) -> Canvas:
        from minebot.panel.views import render

        return render(self.frame(), width, height, self.detail, self.detail_scroll)

    def _log_view(self) -> tuple[str, list[str], int] | None:
        if self.log_index is None:
            return None
        name, attribute = LOG_FILES[self.log_index]
        return name, tail_lines(getattr(self.launcher, attribute), 2000), self.log_scroll

    def _draw_confirm(self, canvas: Canvas) -> None:
        launched = self.launcher.client is not None
        width = min(canvas.width - 4, 64)
        height = 7 if launched else 6
        x, y = (canvas.width - width) // 2, (canvas.height - height) // 2
        canvas.box(x, y, width, height, theme.layer("reflex", bold=True), chars=BOX_HEAVY,
                   title=[("退出 MineBot？", theme.layer("reflex", bold=True))])
        canvas.runs(x + 2, y + 2, [("q", theme.BRIGHT), ("  停止 Agent" + ("，关闭游戏客户端" if launched else ""), theme.TEXT)])
        row = y + 3
        if launched:
            canvas.runs(x + 2, row, [("k", theme.BRIGHT), ("  停止 Agent，保留游戏客户端", theme.TEXT)])
            row += 1
        canvas.runs(x + 2, row, [("esc", theme.BRIGHT), (" 继续运行", theme.TEXT)])

    # ------------------------------------------------------------ actions
    def action_quit(self) -> None:
        if self.stage == "stopping":
            return
        if self.stage == "launch" and self.launcher.phase in ("failed", "launching") and not self.launcher.session_alive() \
                and self.launcher.client is None:
            self._stop(stop_client=True)
            return
        if not self.confirm_quit:
            self.confirm_quit = True
            return
        self._stop(stop_client=True)

    def action_keep_client(self) -> None:
        if self.confirm_quit:
            self._stop(stop_client=False)

    def _stop(self, *, stop_client: bool) -> None:
        self.confirm_quit = False
        self.stage = "stopping"
        self.log_index = None
        self.run_worker(lambda: self.launcher.shutdown(stop_client=stop_client), thread=True, exclusive=True, group="stop")

    def action_close(self) -> None:
        if self.confirm_quit:
            self.confirm_quit = False
            return
        if self.log_index is not None:
            self.log_index = None
            return
        if self.stage == "launch" and self.launcher.phase == "launching":
            self.confirm_quit = True
            return
        super().action_close()

    def action_retry(self) -> None:
        if self.stage == "launch" and self.launcher.phase == "failed":
            self.log_index = None
            self._start_launch()

    def action_log(self) -> None:
        if self.command_open:
            return
        self.log_index = None if self.log_index is not None else (0 if self._log_preference() == 0 else 1)
        self.log_scroll = 0

    def _log_preference(self) -> int:
        current = self.launcher.current
        return 1 if current is not None and current.key in ("client", "world") else 0

    def action_page(self, step: int) -> None:
        if self.log_index is not None:
            self.log_index = (self.log_index + 1) % len(LOG_FILES)
            self.log_scroll = 0
            return
        if self.stage == "panel":
            super().action_page(step)

    def action_goto(self, index: int) -> None:
        if self.stage == "panel":
            super().action_goto(index)

    def action_move(self, step: int) -> None:
        if self.log_index is not None:
            self.log_scroll = max(0, self.log_scroll - step)
            return
        if self.stage == "panel":
            super().action_move(step)

    def action_command(self) -> None:
        if self.stage == "panel":
            super().action_command()

    def action_toggle_pause(self) -> None:
        if self.stage == "panel":
            super().action_toggle_pause()

    def action_cancel_action(self) -> None:
        if self.stage == "panel":
            super().action_cancel_action()


# ------------------------------------------------------------------ plain mode

def run_plain(launcher: Launcher) -> int:
    """Line-oriented launch for scripts: print the steps, then forward stdin lines to the session."""
    printed: dict[str, int] = {}

    def report() -> None:
        for step in launcher.steps:
            seen = printed.get(step.key, 0)
            for tone, text in step.lines[seen:]:
                print(f"[{step.label}] {launch_view.ICONS.get(tone, '·')} {text}", flush=True)
            printed[step.key] = len(step.lines)

    worker = threading.Thread(target=launcher.run, daemon=True)
    worker.start()
    while worker.is_alive():
        report()
        time.sleep(0.2)
    report()
    if launcher.phase != "ready":
        failed = launcher.current
        if failed is not None:
            print(f"[{failed.label}] ✗ {failed.error}", file=sys.stderr)
            if failed.hint:
                print(f"    → {failed.hint}", file=sys.stderr)
        launcher.shutdown()
        return 2
    print(f"ready trace={launcher.trace_path}", flush=True)
    try:
        for line in sys.stdin:
            if not launcher.send(line):
                break
            if line.strip().lower() in ("/quit", "/exit"):
                break
    except KeyboardInterrupt:
        pass
    launcher.shutdown()
    for _, _, text in launcher.messages:
        print(text)
    return launcher.session.returncode if launcher.session and launcher.session.returncode is not None else 0


# ------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minebot",
        description="Start the MineBot client, Agent and live panel with one command.",
        epilog="minebot doctor: run the environment, account and server checks only.  "
               "minebot-panel <run>: replay a finished session.",
    )
    parser.add_argument("goal", nargs="?", help="optional first goal; the session stays open for more")
    parser.add_argument("--env-file", type=Path, help="runtime KEY=VALUE profile (default: discovered like minebot-local)")
    parser.add_argument("--server", help="server HOST:PORT (MINEBOT_CLIENT_SERVER)")
    parser.add_argument("--bot", help="bot account name (MINEBOT_REAL_BOT)")
    parser.add_argument("--port", type=int, help="client IPC port (MINEBOT_REAL_CLIENT_PORT)")
    parser.add_argument("--disposable", action=argparse.BooleanOptionalAction, default=None,
                        help="allow the bot to change the world (local test servers only)")
    parser.add_argument("--client", action=argparse.BooleanOptionalAction, default=True,
                        help="launch the game client when none is running (default: on)")
    parser.add_argument("--run-dir", type=Path, help="session directory (default: logs/sessions/<time>)")
    parser.add_argument("--max-steps", type=int, help="runaway guard passed to the session")
    parser.add_argument("--plain", action="store_true", help="plain text output; stdin lines go to the bot")
    return parser


def options_from(args: argparse.Namespace) -> LaunchOptions:
    return LaunchOptions(goal=args.goal, env_file=args.env_file, run_dir=args.run_dir, server=args.server, bot=args.bot,
                         port=args.port, disposable=args.disposable, launch_client=args.client, max_steps=args.max_steps)


DOCTOR_STEPS = 3  # environment, account, server: everything checkable without starting anything


def doctor(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    options = options_from(args)
    options.create_run_dir = False
    launcher = Launcher(options)
    ok = launcher.run(start=0, stop=DOCTOR_STEPS)
    for step in launcher.steps[:DOCTOR_STEPS]:
        for tone, text in step.lines:
            print(f"[{step.label}] {launch_view.ICONS.get(tone, '·')} {text}")
        if step.status == "fail":
            print(f"[{step.label}] ✗ {step.error}")
            if step.hint:
                print(f"    → {step.hint}")
    host, port = launcher.ipc if launcher.environment else ("127.0.0.1", 0)
    if ok and port:
        from minebot.shell.launch import port_open

        running = port_open(host, port)
        print(f"[客户端] {'✓ IPC ' + host + ':' + str(port) + ' 已有客户端在监听' if running else '· 没有运行中的客户端，minebot 会启动它'}")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["doctor"]:
        return doctor(argv[1:])
    if argv[:1] == ["panel"]:
        from minebot.panel.app import main as panel_main

        return panel_main(argv[1:])
    args = build_parser().parse_args(argv)
    launcher = Launcher(options_from(args))
    if args.plain or not sys.stdout.isatty():
        return run_plain(launcher)
    app = ShellApp(launcher)

    def hangup(*_: object) -> None:
        app.exit()

    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, hangup)
    try:
        app.run()
    finally:
        # Covers crashes and hangups: never leave the Agent or a client we started behind.
        launcher.shutdown(stop_client=True)
    try:
        print(f"MineBot 会话目录 {launcher.run_dir}")
        if launcher.trace_path.exists():
            print(f"回放：minebot-panel {launcher.run_dir}")
        for _, _, text in launcher.messages:
            print(f"  {text}")
    except OSError:
        pass
    return 0 if launcher.phase == "stopped" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ShellApp", "main", "run_plain"]
