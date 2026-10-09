"""``minebot`` launcher: steps, supervision and the panel's control path, against fake processes."""

import asyncio
import json
import os
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
        try:  # like MineBotClientBody.serve: a dropped peer never stops the accept loop
            send(conn, {"type": "ready"})
            while (request := frame(conn)) is not None:
                if request.get("type") == "state":
                    send(conn, {"type": "state", "request_id": request["request_id"], "pos": [1.5, 64.0, -2.5],
                                "health": 20.0, "food": 18, "dimension": "minecraft:overworld",
                                "disposable_world": os.environ.get("MINEBOT_CLIENT_DISPOSABLE_WORLD") == "true"})
        except OSError:
            pass
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


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never read the real ~/.config/minebot, nor MINEBOT_* exported by the developer's shell."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in [key for key in os.environ if key.startswith(("MINEBOT_", "OPENAI_"))]:
        monkeypatch.delenv(key)
    return tmp_path / "config" / "minebot"


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


# ------------------------------------------------------------------ control channel, supervisor, attach

def _supervised(tmp_path, server, port, **options):
    from minebot.shell.supervisor import spawn

    client = tmp_path / "fake_client.py"
    session = tmp_path / "fake_session.py"
    client.write_text(FAKE_CLIENT)
    session.write_text(FAKE_SESSION)
    launch = LaunchOptions(env_file=_profile(tmp_path, server, port), run_dir=tmp_path / "run",
                           client_timeout_s=20, world_timeout_s=10, agent_timeout_s=20, **options)
    process = spawn(launch, client_command=[sys.executable, str(client)],
                    session_command=[sys.executable, str(session)])
    return process, launch.run_dir


def _wait_phase(remote, phases, timeout_s=30):
    deadline = time.monotonic() + timeout_s
    while remote.phase not in phases and time.monotonic() < deadline:
        time.sleep(0.05)
    return remote.phase


def test_supervisor_survives_its_front_end_and_attach_takes_control(tmp_path, fake_server):
    from minebot.shell.control import RemoteLauncher, live_runs

    process, run_dir = _supervised(tmp_path, fake_server, _free_port())
    try:
        first = RemoteLauncher(run_dir)
        first.connect()
        assert _wait_phase(first, ("ready", "failed")) == "ready", [(s.label, s.error) for s in first.steps]
        assert first.bot == "MineBotCombat" and first.client_started
        assert first.send("第一句")
        first.close()  # the panel goes away: the bot must keep running
        time.sleep(0.5)
        assert process.poll() is None
        second = RemoteLauncher(run_dir)
        second.connect()
        assert second.phase == "ready" and second.session_alive()
        assert second.send("/pause") and second.agent_paused
        second.refresh()
        assert second.agent_paused, "pause state lives in the supervisor"
        assert second.send("第二句")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sum(r["event"] == "user_message" for r in _records(run_dir / "trace.jsonl")) < 3:
            time.sleep(0.05)
        second.shutdown()
        assert second.phase == "stopped"
        assert any("客户端已关闭" in text for _, _, text in second.messages)
        second.close()
        assert process.wait(timeout=15) == 0
        contents = [r.get("content") for r in _records(run_dir / "trace.jsonl") if r["event"] == "user_message"]
        assert contents == ["第一句", "/pause", "第二句", "/quit"]
        log = [json.loads(line) for line in (run_dir / "control.log").read_text().splitlines()]
        assert [entry["op"] for entry in log] == ["send", "send", "send", "stop"]
        assert not (run_dir / "control.sock").exists()
        assert live_runs(tmp_path) == []
        record = json.loads((run_dir / "launch.json").read_text())
        assert record["phase"] == "stopped" and "sk-test" not in json.dumps(record)
    finally:
        if process.poll() is None:
            process.kill()


def test_supervisor_resolves_relative_paths_from_the_callers_directory(tmp_path, monkeypatch):
    from minebot.shell.supervisor import OPTIONS_NAME, read_options, spawn

    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel.env").write_text("MINEBOT_REAL_BOT=Bot1\n")
    options = LaunchOptions(env_file=Path("rel.env"), run_dir=tmp_path / "run", server="127.0.0.1:1")
    process = spawn(options)
    try:
        saved, _, _ = read_options(tmp_path / "run" / OPTIONS_NAME)
        assert saved.env_file == tmp_path / "rel.env"
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_supervisor_reports_launch_failure_remotely(tmp_path):
    from minebot.shell.control import RemoteLauncher

    process, run_dir = _supervised(tmp_path, f"127.0.0.1:{_free_port()}", _free_port())
    try:
        remote = RemoteLauncher(run_dir)
        remote.connect()
        assert _wait_phase(remote, ("failed",)) == "failed"
        assert remote.current.key == "server" and remote.current.hint
        assert not remote.send("hi"), "no session to talk to yet"
        assert remote.run(), "retry is accepted after a failure"
        assert _wait_phase(remote, ("failed",)) == "failed"
        remote.shutdown()
        assert remote.phase == "stopped"
        remote.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()


def test_supervisor_without_front_end_stops_after_launch_failure(tmp_path):
    from minebot.shell.supervisor import Supervisor

    launcher = _launcher(tmp_path, "example.com:25565", _free_port())
    supervisor = Supervisor(launcher, orphan_s=0.3)
    started = time.monotonic()
    assert supervisor.serve() == 0
    assert launcher.phase == "stopped" and time.monotonic() - started < 10
    assert not (launcher.run_dir / "control.sock").exists()


def test_control_socket_survives_long_run_dirs(tmp_path):
    from minebot.shell.control import ControlClient, ControlServer

    class Stub:
        run_dir = tmp_path / ("x" * 90) / "run"
        phase = "ready"

        def snapshot(self):
            return {"phase": self.phase}

        def send(self, line):
            return line == "ok"

    Stub.run_dir.mkdir(parents=True)
    server = ControlServer(Stub())
    server.start()
    try:
        client = ControlClient(Stub.run_dir)
        client.connect()
        assert client.request("snapshot")["snapshot"] == {"phase": "ready"}
        assert client.request("send", "ok")["ok"]
        reply = client.request("send", "nope")
        assert not reply["ok"] and reply["error"]
        assert not client.request("bogus")["ok"]
        client.close()
    finally:
        server.close()
    assert not (Stub.run_dir / "control.sock").exists()


def test_shell_app_detaches_and_reattaches(tmp_path, fake_server):
    from minebot.shell.control import RemoteLauncher

    process, run_dir = _supervised(tmp_path, fake_server, _free_port())

    async def drive(remote, keys):
        app = ShellApp(remote)
        async with app.run_test(size=(120, 40)) as pilot:
            deadline = time.monotonic() + 30
            while app.stage != "panel" and time.monotonic() < deadline:
                await pilot.pause(0.1)
            assert app.stage == "panel", [(s.label, s.status, s.error) for s in remote.steps]
            for key in keys:
                await pilot.press(key)
                await pilot.pause(0.05)
            deadline = time.monotonic() + 20
            while app.is_running and time.monotonic() < deadline:
                await pilot.pause(0.1)
        return app

    try:
        first = RemoteLauncher(run_dir)
        first.connect()
        app = asyncio.run(drive(first, ["colon", "h", "i", "enter", "ctrl+d"]))
        first.close()
        assert app.detached and process.poll() is None
        second = RemoteLauncher(run_dir)
        second.connect()
        app = asyncio.run(drive(second, ["p", "q", "q"]))
        second.close()
        assert not app.detached and second.phase == "stopped"
        assert process.wait(timeout=15) == 0
        contents = [r.get("content") for r in _records(run_dir / "trace.jsonl") if r["event"] == "user_message"]
        assert contents == ["hi", "/pause", "/quit"]
    finally:
        if process.poll() is None:
            process.kill()


def test_foreground_panel_cannot_detach(tmp_path, fake_server):
    launcher = _launcher(tmp_path, fake_server, _free_port())

    async def drive():
        app = ShellApp(launcher)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("ctrl+d")
            assert not app.detached and app.flash is not None and "不能分离" in app.flash[1]
        launcher.shutdown()

    asyncio.run(drive())


# ------------------------------------------------------------------ server profiles and bot accounts

def test_server_profile_supplies_address_account_and_world(tmp_path, fake_server, isolated_config):
    from minebot.shell.profiles import ServerBook, ServerProfile

    book = ServerBook()
    book.put(ServerProfile("home", fake_server, account="ProfileBot", world="disposable", goal="砍树"))
    book.save()
    assert ServerBook().get(None).account == "ProfileBot"
    launcher = _launcher(tmp_path, "127.0.0.1:1", _free_port())
    launcher.options.server = "home"  # `minebot --server home` names a profile
    assert launcher.run(start=0, stop=3), [(s.label, s.error) for s in launcher.steps]
    assert launcher.server == fake_server and launcher.bot == "ProfileBot" and launcher.disposable
    assert launcher.options.goal == "砍树" and launcher.profile_name == "home"
    flagged = _launcher(tmp_path, "127.0.0.1:1", _free_port(), profile="home", bot="FlagBot", disposable=False)
    assert flagged.run(start=0, stop=3), [(s.label, s.error) for s in flagged.steps]
    assert flagged.bot == "FlagBot" and not flagged.disposable


@pytest.mark.parametrize("table, message", [
    ('[x]\naddress = "example.com:25565"\nworld = "disposable"\n', "本地"),
    ('[x]\naddress = "127.0.0.1:1"\nworld = "shared"\n', "shared"),
    ('[x]\naddress = "127.0.0.1:1"\nwrold = "protected"\n', "wrold"),
    ('default = "y"\n[x]\naddress = "127.0.0.1:1"\n', "default"),
])
def test_bad_server_profiles_fail_the_environment_step(tmp_path, isolated_config, table, message):
    isolated_config.mkdir(parents=True)
    (isolated_config / "servers.toml").write_text(table)
    launcher = _launcher(tmp_path, "127.0.0.1:1", _free_port())
    assert not launcher.run(stop=1)
    assert launcher.steps[0].status == "fail" and message in launcher.steps[0].error


def _xbox_transport(calls, *, pending=1, has_game=True):
    import httpx

    state = {"pending": pending}

    def handle(request):
        url = str(request.url)
        calls.append(url)
        form = dict(httpx.QueryParams(request.content.decode())) if request.method == "POST" and \
            request.headers.get("content-type", "").startswith("application/x-www-form") else {}
        if url.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-1234", "interval": 1,
                                             "expires_in": 900, "verification_uri": "https://microsoft.com/link"})
        if url.endswith("/token"):
            if form.get("grant_type") == "refresh_token":
                assert form["refresh_token"].startswith("msa-refresh-")
                rotated = int(form["refresh_token"].rsplit("-", 1)[1]) + 1
                return httpx.Response(200, json={"access_token": f"msa-access-{rotated}", "refresh_token": f"msa-refresh-{rotated}"})
            if state["pending"]:
                state["pending"] -= 1
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "msa-access-1", "refresh_token": "msa-refresh-1"})
        if "user.auth.xboxlive.com" in url:
            return httpx.Response(200, json={"Token": "xbl", "DisplayClaims": {"xui": [{"uhs": "hash"}]}})
        if "xsts.auth.xboxlive.com" in url:
            return httpx.Response(200, json={"Token": "xsts"})
        if url.endswith("/login_with_xbox"):
            assert json.loads(request.content)["identityToken"] == "XBL3.0 x=hash;xsts"
            return httpx.Response(200, json={"access_token": f"mc-token-{len(calls)}", "expires_in": 86400})
        if url.endswith("/minecraft/profile"):
            if not has_game:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"id": "0123456789abcdef0123456789abcdef", "name": "OfficialBot"})
        return httpx.Response(500)

    return httpx.MockTransport(handle)


def test_official_login_refreshes_and_only_the_client_gets_the_token(tmp_path, fake_server, isolated_config):
    from minebot.shell.accounts import AccountStore

    calls, shown = [], []
    clock = [1000.0]
    store = AccountStore(transport=_xbox_transport(calls), clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    account = store.login("client-id", announce=lambda url, code, ttl: shown.append(code))
    assert shown == ["ABCD-1234"] and account.name == "OfficialBot" and account.kind == "msa"
    path = isolated_config / "accounts" / "OfficialBot.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.ensure_fresh("OfficialBot").access_token == account.access_token, "fresh tokens are reused"
    clock[0] += 86400
    refreshed = store.ensure_fresh("OfficialBot")
    assert refreshed.access_token != account.access_token and refreshed.refresh_token == "msa-refresh-2"

    env_dump = tmp_path / "client-env.json"
    dump = tmp_path / "dump_client.py"
    dump.write_text(f"import json, os\njson.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n" + FAKE_CLIENT)
    launcher = _launcher(tmp_path, fake_server, _free_port(), bot="OfficialBot")
    launcher._launch_client_command = [sys.executable, str(dump)]
    import minebot.shell.launch as launch_module
    original = launch_module.AccountStore
    launch_module.AccountStore = lambda directory=None: AccountStore(directory, transport=_xbox_transport([]))
    try:
        assert launcher.run(), [(s.label, s.error) for s in launcher.steps]
    finally:
        launch_module.AccountStore = original
    try:
        assert launcher.account_kind == "msa" and "正版" in launcher.steps[1].summary
        client_env = json.loads(env_dump.read_text())
        assert client_env["MINEBOT_CLIENT_ACCESS_TOKEN"].startswith("mc-token")
        assert client_env["MINEBOT_CLIENT_UUID"] == "0123456789abcdef0123456789abcdef"
        assert "MINEBOT_CLIENT_ACCESS_TOKEN" not in launcher.environment, "the Agent session must not see the token"
    finally:
        launcher.shutdown()
    token = client_env["MINEBOT_CLIENT_ACCESS_TOKEN"]
    for name in ("launch.json", "trace.jsonl", "agent.log"):
        assert token not in (launcher.run_dir / name).read_text(), name


def test_shell_variables_overriding_the_profile_are_called_out(tmp_path, fake_server, monkeypatch):
    monkeypatch.setenv("MINEBOT_LLM_MODEL", "from-shell")
    launcher = _launcher(tmp_path, fake_server, _free_port())
    assert launcher.run(start=0, stop=1)
    assert launcher.steps[0].status == "warn" and launcher.model == "from-shell"
    assert any("MINEBOT_LLM_MODEL" in text for tone, text in launcher.steps[0].lines if tone == "warn")


def test_offline_bot_launch_creates_no_account_files(tmp_path, fake_server, isolated_config):
    launcher = _launcher(tmp_path, fake_server, _free_port())
    assert launcher.run(start=0, stop=2)
    assert launcher.account_kind == "offline" and not (isolated_config / "accounts").exists()


def test_official_login_without_the_game_explains_why(tmp_path, isolated_config):
    from minebot.shell.accounts import AccountError, AccountStore

    store = AccountStore(transport=_xbox_transport([], pending=0, has_game=False), sleep=lambda s: None)
    with pytest.raises(AccountError, match="没有购买"):
        store.login("client-id", announce=lambda *args: None)
    assert store.names() == []


def test_expired_login_fails_the_account_step_with_a_fix(tmp_path, isolated_config):
    import httpx

    from minebot.shell.accounts import Account, AccountStore

    AccountStore().save(Account("OfficialBot", "u" * 32, client_id="c", refresh_token="r"))
    launcher = _launcher(tmp_path, "127.0.0.1:1", _free_port(), bot="OfficialBot")
    import minebot.shell.launch as launch_module
    original = launch_module.AccountStore
    expired = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    launch_module.AccountStore = lambda directory=None: AccountStore(directory, transport=expired)
    try:
        assert not launcher.run(stop=2)
    finally:
        launch_module.AccountStore = original
    step = launcher.steps[1]
    assert step.status == "fail" and "invalid_grant" in step.error and "minebot login" in step.hint


# ------------------------------------------------------------------ themes, help, small terminals

@pytest.mark.parametrize("name", ["dark", "light", "contrast", "mono"])
def test_every_theme_restyles_the_whole_canvas(name):
    from minebot.panel import theme

    canvas = Canvas(40, 3)
    canvas.box(0, 0, 40, 3, theme.layer("agent", bold=True), title=[("标题", theme.layer("agent", dim=True))])
    canvas.text(2, 1, "ok", theme.OK)
    canvas.text(6, 1, "muted", theme.MUTED)
    canvas.text(14, 1, "row", theme.on("agent"))
    try:
        theme.set_theme(name)
        spans = canvas.to_text().spans
        colors = {span.style.color.triplet.hex for span in spans if span.style and span.style.color}
        if name == "mono":
            assert not colors, "NO_COLOR must not emit colours"
            assert any(span.style.reverse for span in spans if span.style), "highlights use reverse video"
        elif name == "light":
            assert colors and not colors & {"#87d787", "#5fd75f", "#6c7a89"}, colors
        assert theme.background() == {"dark": "#0b0f14", "light": "#f6f8fa", "contrast": "#000000",
                                      "mono": "#0b0f14"}[name]
    finally:
        theme.set_theme("dark")


def test_no_color_selects_mono():
    from minebot.panel import theme

    assert theme.default_theme({"NO_COLOR": "1", "MINEBOT_THEME": "light"}) == "mono"
    assert theme.default_theme({"MINEBOT_THEME": "contrast"}) == "contrast"
    assert theme.default_theme({}) == "dark"


def test_panel_help_theme_and_small_terminal(tmp_path, isolated_config, monkeypatch):
    from minebot.panel import prefs, theme

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("MINEBOT_THEME", raising=False)
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({"seq": 1, "ts": time.time(), "event": "interactive_ready"}) + "\n")

    async def drive():
        app = PanelApp(trace, replay=False, root=None)
        async with app.run_test(size=(120, 40)) as pilot:
            assert app.flash is not None and "?" in app.flash[1], "first opens show the help hint"
            await pilot.press("question_mark")
            assert "快捷键" in app.draw(120, 40).plain
            assert "暂停 / 继续" not in app.draw(120, 40).plain, "read-only panels don't list control keys"
            await pilot.press("escape")
            assert not app.show_help
            await pilot.press("T")
            assert theme.active_theme() == "light"
            await pilot.press("3")
            assert "放大终端" in app.draw(60, 20).plain

    try:
        asyncio.run(drive())
        saved = prefs.load()
        assert saved["theme"] == "light" and saved["page"] == 2 and saved["help_hints"] == 1
        theme.set_theme("dark")
        PanelApp(trace, replay=False, root=None)
        assert theme.active_theme() == "light", "the theme is remembered"
    finally:
        theme.set_theme("dark")


# ------------------------------------------------------------------ notices and the Agent status

def test_notices_merge_repeats_and_flag_what_needs_attention():
    from minebot.panel.notices import Notifier

    notifier = Notifier()
    failed = {"event": "tool_result", "tool": "get_to_block", "full_result": {"success": False, "reason": "no_path"}}
    for offset in (0, 5, 10):
        notifier.observe({**failed, "ts": 100.0 + offset})
    notifier.observe({"event": "tool_result", "ts": 111, "tool": "mine", "full_result": {"success": True}})
    notifier.observe({"event": "body_state", "ts": 112, "health": 20})
    notifier.observe({"event": "body_state", "ts": 113, "health": 19})
    notifier.observe({"event": "body_state", "ts": 114, "health": 12})
    notifier.observe({"event": "world_edit", "ts": 115, "target": False})
    notifier.observe({"event": "assistant_message", "ts": 116, "content": "我在砍树"})
    notifier.observe({"event": "session_terminal", "ts": 117, "status": "quit"})
    labels = [notice.label() for notice in notifier.history]
    assert labels == ["✗ get_to_block：no_path ×3", "♥ 掉血 19 → 12", "⚠ 改动了目标范围外的方块", "会话结束：quit"]
    assert len(notifier.visible()) == 3 and notifier.unread == 6


def test_live_panel_shows_new_notices_but_not_old_ones(tmp_path):
    trace = tmp_path / "trace.jsonl"
    old = {"seq": 1, "ts": time.time(), "event": "tool_result", "tool": "old_tool", "full_result": {"success": False}}
    trace.write_text(json.dumps(old) + "\n")

    async def drive():
        app = PanelApp(trace, replay=False, root=None)
        async with app.run_test(size=(140, 40)) as pilot:
            assert app.notifier.visible() == [] and len(app.notifier.history) == 1
            with open(trace, "a") as handle:
                handle.write(json.dumps({**old, "seq": 2, "tool": "new_tool", "ts": time.time()}) + "\n")
            await pilot.pause(0.4)
            assert [notice.text for notice in app.notifier.visible()] == ["✗ new_tool"]
            assert "new_tool" in app.draw(140, 40).plain
            await pilot.press("N")
            text = app.draw(140, 40).plain
            assert "old_tool" in text and "new_tool" in text and app.notifier.unread == 0

    asyncio.run(drive())


def test_agent_status_reads_the_trace():
    from minebot.panel.model import PanelState, apply
    from minebot.panel.views.common import Frame, agent_status

    state = PanelState()
    now = time.time()

    def status(control=None):
        return agent_status(Frame(state=state, now=now, anim=0.0, live=True, control=control))[0]

    assert status() == "等待指令"
    apply(state, {"seq": 1, "ts": now, "event": "responses_http_attempt", "attempt": 1})
    assert status() == "思考中"
    apply(state, {"seq": 2, "ts": now, "event": "tool_invoke", "tool": "mine", "tool_call_id": "c1"})
    assert status() == "行动中"
    assert status("已暂停") == "已暂停"
    apply(state, {"seq": 3, "ts": now, "event": "session_terminal", "status": "quit"})
    assert status() == "已结束"


# ------------------------------------------------------------------ client crash and restart

def test_crashed_client_is_reported_and_restarted_on_request(tmp_path, fake_server):
    import os as _os
    import signal as _signal

    launcher = _launcher(tmp_path, fake_server, _free_port())

    async def drive():
        app = ShellApp(launcher)
        async with app.run_test(size=(120, 40)) as pilot:
            deadline = time.monotonic() + 30
            while app.stage != "panel" and time.monotonic() < deadline:
                await pilot.pause(0.1)
            assert app.stage == "panel"
            first = launcher.client
            _os.killpg(first.pid, _signal.SIGKILL)
            first.wait(timeout=5)
            deadline = time.monotonic() + 5
            while not app._client_down and time.monotonic() < deadline:
                await pilot.pause(0.1)
            assert app.flash is not None and "r 重启客户端" in app.flash[1]
            assert launcher.session_alive(), "the Agent keeps running without its client"
            await pilot.press("r")
            deadline = time.monotonic() + 20
            while (app._client_restarting or app._client_down) and time.monotonic() < deadline:
                await pilot.pause(0.1)
            assert launcher.client is not first and launcher.client_alive
            assert app.flash is not None and "已重新启动" in app.flash[1]
            assert launcher.phase == "ready" and launcher.steps[3].status == "ok"
        launcher.shutdown()

    asyncio.run(drive())
    assert launcher.client.poll() is not None


def test_reused_client_is_never_restarted(tmp_path, fake_server):
    from minebot.shell.control import ControlError, ControlServer

    launcher = _launcher(tmp_path, fake_server, _free_port())
    launcher.client_reused = True
    server = ControlServer(launcher, on_restart_client=lambda: None)
    with pytest.raises(ControlError, match="客户端还在运行"):
        server.apply("restart_client")
    assert not launcher.restart_client()


def test_client_log_disconnects_and_rejoins_drive_the_connection_state(tmp_path):
    from minebot.panel.model import PanelState
    from minebot.panel.views.common import Frame, agent_status

    launcher = Launcher(LaunchOptions(run_dir=tmp_path / "run"))
    launcher.run_dir.mkdir(parents=True)
    log = launcher.client_log
    assert launcher.watch_client_log()["state"] == "unknown"
    log.write_text('[10:56:55] [Render thread/INFO] (minebot-client-body) MINEBOT_PHASE_A ready {"protocol":"x"}\n')
    assert launcher.watch_client_log()["state"] == "connected"
    with open(log, "a") as handle:
        handle.write("[12:30:29] [Render thread/WARN] (Minecraft) Client disconnected with reason: Timed out\n")
        handle.write("[12:30:30] partial line without newline")
    connection = launcher.watch_client_log()
    assert connection["state"] == "lost" and connection["reason"] == "Timed out"
    assert launcher.snapshot()["connection"]["state"] == "lost"
    frame = Frame(state=PanelState(), now=time.time(), anim=0.0, live=True, connection=connection)
    assert agent_status(frame)[0] == "已断线" and "Timed out" in agent_status(frame)[1]
    with open(log, "a") as handle:
        handle.write("\n[12:31:02] MINEBOT_PHASE_A ready {}\n")
    assert launcher.watch_client_log()["state"] == "connected"
    log.write_text("")  # a new client.log after restart starts from the top
    assert launcher.watch_client_log()["state"] == "connected"
