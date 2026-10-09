"""``minebot`` launcher: steps, supervision and the panel's control path, against fake processes."""

import asyncio
import json
import socket
import struct
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("textual")

from minebot.panel.app import PanelApp  # noqa: E402
from minebot.panel.canvas import Canvas  # noqa: E402
from minebot.panel.views import launch as launch_view  # noqa: E402
from minebot.shell.app import ShellApp, doctor  # noqa: E402
from minebot.shell.launch import LaunchOptions, Launcher, shell_environment  # noqa: E402
from minebot.shell.slp import ping  # noqa: E402

FAKE_CLIENT = textwrap.dedent('''
    import json, os, socket, struct, sys
    port = int(os.environ["MINEBOT_CLIENT_IPC_PORT"])
    print("MINEBOT_PHASE_A client_body_loaded minecraft=26.1.2", flush=True)
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    print(f"MINEBOT_PHASE_A ipc_listening port={port}", flush=True)
    def send(conn, message):
        raw = json.dumps(message).encode()
        conn.sendall(struct.pack(">I", len(raw)) + raw)
    def frame(conn):
        header = conn.recv(4)
        if len(header) < 4:
            return None
        length = struct.unpack(">I", header)[0]
        data = b""
        while len(data) < length:
            chunk = conn.recv(length - len(data))
            if not chunk:
                return None
            data += chunk
        return json.loads(data)
    while True:
        conn, _ = server.accept()
        send(conn, {"type": "ready"})
        while (request := frame(conn)) is not None:
            if request.get("type") == "state":
                send(conn, {"type": "state", "request_id": request["request_id"], "pos": [1.5, 64.0, -2.5],
                            "health": 20.0, "food": 18, "dimension": "minecraft:overworld",
                            "disposable_world": os.environ.get("MINEBOT_CLIENT_DISPOSABLE_WORLD") == "true"})
        conn.close()
''')

FAKE_SESSION = textwrap.dedent('''
    import json, os, sys, time
    path = os.environ["MINEBOT_AGENT_LOG_PATH"]
    seq = 0
    def emit(event, **fields):
        global seq
        seq += 1
        with open(path, "a") as handle:
            handle.write(json.dumps({"seq": seq, "ts": time.time(), "session_id": os.environ["MINEBOT_REAL_BOT"],
                                     "event": event, **fields}) + "\\n")
    assert not any("RCON" in key for key in os.environ), "RCON leaked into the session"
    emit("provider_manifest", default_route="primary")
    emit("runtime_scope", world_id="w")
    emit("interactive_ready", body_provider="real_client", server="127.0.0.1")
    print("interactive_ready", flush=True)
    for line in sys.stdin:
        text = line.strip()
        emit("user_message", command="message", content=text)
        if text == "/quit":
            emit("session_terminal", mode="interactive", status="quit")
            break
''')


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


@pytest.fixture
def fake_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    status = json.dumps({"version": {"name": "26.1.2", "protocol": 999},
                         "players": {"online": 1, "max": 20, "sample": [{"name": "Jess", "id": "0"}]},
                         "description": {"text": "MineBot ", "extra": [{"text": "test"}]}}).encode()

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.recv(256)
                body = _varint(0) + _varint(len(status)) + status
                conn.sendall(_varint(len(body)) + body)

    threading.Thread(target=serve, daemon=True).start()
    yield f"127.0.0.1:{server.getsockname()[1]}"
    server.close()


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _profile(tmp_path, server, port, **extra):
    lines = {
        "MINEBOT_REAL_BOT": "MineBotCombat",
        "MINEBOT_CLIENT_SERVER": server,
        "MINEBOT_REAL_CLIENT_PORT": str(port),
        "MINEBOT_CLIENT_DISPOSABLE_WORLD": "true",
        "MINEBOT_LLM_MODEL": "gpt-test",
        "MINEBOT_LLM_KIND": "openai_responses",
        "MINEBOT_LLM_API_KEY": "sk-test",
        "MINEBOT_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "MINEBOT_REAL_RCON_PASSWORD": "secret",
        **extra,
    }
    path = tmp_path / "runtime.env"
    path.write_text("".join(f"{key}={value}\n" for key, value in lines.items()))
    return path


def _launcher(tmp_path, server, port, **options):
    client = tmp_path / "fake_client.py"
    session = tmp_path / "fake_session.py"
    client.write_text(FAKE_CLIENT)
    session.write_text(FAKE_SESSION)
    launch = LaunchOptions(env_file=_profile(tmp_path, server, port), run_dir=tmp_path / "run",
                           client_timeout_s=20, world_timeout_s=10, agent_timeout_s=20, **options)
    return Launcher(launch, launch_client_command=[sys.executable, str(client)],
                    session_command=[sys.executable, str(session)])


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_server_list_ping_reads_version_players_and_motd(fake_server):
    info = ping(fake_server)
    assert info["version"] == "26.1.2" and info["online"] == 1 and info["names"] == ["Jess"]
    assert info["motd"] == "MineBot test"


def test_shell_environment_agrees_on_bot_and_port_and_drops_rcon():
    environment = shell_environment({"MINEBOT_CLIENT_USERNAME": "Bot2", "MINEBOT_CLIENT_IPC_PORT": "8770",
                                     "MINEBOT_REAL_RCON_HOST": "x"}, LaunchOptions(disposable=False))
    assert environment["MINEBOT_REAL_BOT"] == environment["MINEBOT_CLIENT_USERNAME"] == "Bot2"
    assert environment["MINEBOT_REAL_CLIENT_PORT"] == environment["MINEBOT_CLIENT_IPC_PORT"] == "8770"
    assert environment["MINEBOT_BODY_PROVIDER"] == "real_client"
    assert environment["MINEBOT_CLIENT_DISPOSABLE_WORLD"] == "false"
    assert not any("RCON" in key for key in environment)


def test_full_launch_controls_the_session_and_stops_everything(tmp_path, fake_server):
    launcher = _launcher(tmp_path, fake_server, _free_port())
    assert launcher.run(), [(step.label, step.error) for step in launcher.steps]
    assert [step.status for step in launcher.steps] == ["ok"] * 6
    assert launcher.server_info["version"] == "26.1.2"
    assert launcher.body["pos"] == [1.5, 64.0, -2.5]
    client_notes = [text for _, text in launcher.steps[3].lines]
    assert "mod 已加载" in client_notes and "IPC 开始监听" in client_notes
    assert launcher.send("去砍点木头")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(
            record["event"] == "user_message" for record in _records(launcher.trace_path)):
        time.sleep(0.05)
    launcher.shutdown()
    events = [(record["event"], record.get("content")) for record in _records(launcher.trace_path)]
    assert ("user_message", "去砍点木头") in events and ("user_message", "/quit") in events
    assert launcher.session.returncode == 0
    assert launcher.client.poll() is not None, "the client this launcher started must be stopped"
    assert launcher.phase == "stopped"
    record = json.loads((launcher.run_dir / "launch.json").read_text())
    assert record["phase"] == "stopped" and len(record["steps"]) == 6
    assert "sk-test" not in (launcher.run_dir / "launch.json").read_text()


def test_existing_client_is_reused_and_left_running(tmp_path, fake_server):
    port = _free_port()
    first = _launcher(tmp_path, fake_server, port)
    assert first.run(start=0, stop=4)
    second = _launcher(tmp_path, fake_server, port)
    second.run_dir = tmp_path / "run2"
    assert second.run(start=0, stop=5)
    assert second.client_reused and second.client is None
    second.shutdown()
    assert first.client.poll() is None
    first.shutdown()


def test_unreachable_server_fails_its_step_and_retry_resumes_there(tmp_path, fake_server):
    port = _free_port()
    launcher = _launcher(tmp_path, f"127.0.0.1:{_free_port()}", port)
    assert not launcher.run()
    assert [step.status for step in launcher.steps[:3]] == ["ok", "ok", "fail"]
    assert launcher.steps[2].hint and launcher.phase == "failed"
    launcher.environment["MINEBOT_CLIENT_SERVER"] = fake_server
    assert launcher.run(stop=3)
    assert launcher.steps[2].status == "ok"


def test_client_that_dies_reports_its_log(tmp_path, fake_server):
    launcher = _launcher(tmp_path, fake_server, _free_port())
    crash = tmp_path / "crash.py"
    crash.write_text("print('ERROR Mixin apply failed WorldEditMixin', flush=True)\nraise SystemExit(3)\n")
    launcher._launch_client_command = [sys.executable, str(crash)]
    assert not launcher.run()
    step = launcher.steps[3]
    assert step.status == "fail" and "代码 3" in step.error and "Mixin apply failed" in step.error


def test_disposable_world_is_refused_for_remote_servers(tmp_path):
    launcher = _launcher(tmp_path, "example.com:25565", _free_port())
    assert not launcher.run()
    assert launcher.steps[2].status == "fail" and "本地" in launcher.steps[2].error


def test_doctor_checks_without_creating_a_session(tmp_path, fake_server, capsys):
    code = doctor(["--env-file", str(_profile(tmp_path, fake_server, _free_port())), "--run-dir", str(tmp_path / "none")])
    output = capsys.readouterr().out
    assert code == 0 and "26.1.2" in output and "MineBotCombat" in output
    assert not (tmp_path / "none").exists()


@pytest.mark.parametrize("size", [(80, 30), (120, 40), (200, 60)])
def test_launch_screen_renders_every_phase(tmp_path, fake_server, size):
    launcher = _launcher(tmp_path, fake_server, _free_port())
    launcher.environment = {"MINEBOT_REAL_BOT": "MineBotCombat", "MINEBOT_CLIENT_SERVER": fake_server}
    steps = launcher.steps
    steps[0].status, steps[0].summary, steps[0].started, steps[0].ended = "ok", "全部通过", 1.0, 1.4
    steps[0].note("ok", "模型 gpt-test")
    steps[1].status, steps[1].summary = "running", "MineBotCombat"
    steps[1].started = time.monotonic()
    launcher.output.extend(["[client] Loading 312 mods...", "很长的中文日志" * 40])
    for phase in ("launching", "failed", "stopping"):
        if phase == "failed":
            steps[1].status, steps[1].error, steps[1].hint = "fail", "连接被拒绝：" + "x" * 300, "检查服务器"
        launcher.phase = phase
        launcher.say("ok", "Agent 已退出（代码 0）")
        canvas = Canvas(*size)
        launch_view.draw(canvas, launcher, 0.3)
        rows = canvas.plain_rows()
        assert len(rows) == size[1]
        text = "\n".join(rows)
        assert "环境" in text and "Agent" in text
        if phase == "failed":
            assert "r 重试这一步" in text
        if phase == "stopping":
            assert "正在收尾" in text


def test_shell_app_launches_into_the_panel_and_sends_operator_lines(tmp_path, fake_server):
    launcher = _launcher(tmp_path, fake_server, _free_port())

    async def drive():
        app = ShellApp(launcher)
        async with app.run_test(size=(120, 40)) as pilot:
            deadline = time.monotonic() + 30
            while app.stage != "panel" and time.monotonic() < deadline:
                await pilot.pause(0.1)
            assert app.stage == "panel", [(step.label, step.status, step.error) for step in launcher.steps]
            await pilot.press("colon")
            assert app.command_open
            for key in "hi q1":  # app keys must not fire while typing
                await pilot.press("space" if key == " " else key)
            await pilot.press("enter")
            assert not app.command_open and app.page == 0
            await pilot.press("p")
            assert app.agent_paused
            await pilot.press("exclamation_mark")
            await pilot.press("exclamation_mark")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(app.state.operator) < 3:
                await pilot.pause(0.1)
            assert [entry["content"] for entry in app.state.operator][:3] == ["hi q1", "/pause", "/cancel panel_cancel"]
            await pilot.press("q")
            assert app.confirm_quit
            await pilot.press("q")
            deadline = time.monotonic() + 20
            while launcher.phase != "stopped" and time.monotonic() < deadline:
                await pilot.pause(0.1)
        return app

    asyncio.run(drive())
    assert launcher.phase == "stopped"
    assert launcher.session.returncode == 0 and launcher.client.poll() is not None


def test_read_only_panel_refuses_control(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({"seq": 1, "ts": time.time(), "event": "interactive_ready"}) + "\n")

    async def drive():
        app = PanelApp(trace, replay=False, root=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("colon")
            assert not app.command_open
            assert app.flash is not None and "只读" in app.flash[1]

    asyncio.run(drive())
