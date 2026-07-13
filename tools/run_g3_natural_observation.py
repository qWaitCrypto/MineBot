#!/usr/bin/env python3
"""Run G3 natural-terrain observation against an isolated local server."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.config import AppConfigError, agent_language_from_env, provider_registry_from_env  # noqa: E402
from minebot.app.observability import JsonlObservationSink  # noqa: E402
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime  # noqa: E402
from minebot.app.real_server_session import safe_evaluate_terminal_truth  # noqa: E402
from minebot.app.runner import RuntimeTrace  # noqa: E402
from minebot.app.session import AgentSession, SessionCommand  # noqa: E402
from minebot.contract import Region  # noqa: E402
from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.errors import RconError  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "test-server"
LOG_DIR = ROOT / "logs" / "g3-natural-terrain"
GOAL = "craft an iron pickaxe"
BOT = "G3NaturalBot"
REGION = Region("g3-natural-world", (-512, -64, -512), (512, 320, 512))

HOSTILE_TYPES = (
    "blaze",
    "cave_spider",
    "creeper",
    "drowned",
    "elder_guardian",
    "enderman",
    "endermite",
    "evoker",
    "ghast",
    "guardian",
    "hoglin",
    "husk",
    "magma_cube",
    "phantom",
    "piglin_brute",
    "pillager",
    "ravager",
    "shulker",
    "silverfish",
    "skeleton",
    "slime",
    "spider",
    "stray",
    "vex",
    "vindicator",
    "warden",
    "witch",
    "wither_skeleton",
    "zoglin",
    "zombie",
    "zombie_villager",
)


@dataclass(frozen=True)
class ServerSpec:
    root: Path
    host: str
    server_port: int
    rcon_port: int
    password: str

    @property
    def world(self) -> Path:
        return self.root / "world"

    @property
    def golden(self) -> Path:
        return self.root / "world-g3-golden"

    @property
    def latest_log(self) -> Path:
        return self.root / "logs" / "latest.log"

    @property
    def process_log(self) -> Path:
        return self.root / "logs" / "g3-server.log"

    @property
    def rcon(self) -> RconConfig:
        return RconConfig(host=self.host, port=self.rcon_port, password=self.password, timeout_s=20.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="G3 natural-terrain observation runner.")
    parser.add_argument("--mode", choices=("llm",), default="llm")
    parser.add_argument("--workdir", type=Path, default=Path("/tmp") / f"minebot-g3-natural-{int(time.time())}")
    parser.add_argument("--server-port", type=int, default=_free_port(25666))
    parser.add_argument("--rcon-port", type=int, default=_free_port(25676))
    parser.add_argument("--password", default="test-g3")
    parser.add_argument("--seed", default="", help="Empty string means Minecraft picks a fresh natural seed.")
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--keep-server-dir", action="store_true")
    args = parser.parse_args()

    spec = ServerSpec(
        root=args.workdir.resolve(),
        host="127.0.0.1",
        server_port=args.server_port,
        rcon_port=args.rcon_port,
        password=args.password,
    )
    modes = (args.mode,)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started: subprocess.Popen[str] | None = None
    summary: dict[str, object] = {
        "goal": GOAL,
        "server_dir": str(spec.root),
        "server_port": spec.server_port,
        "rcon_port": spec.rcon_port,
        "seed": args.seed or "random",
        "runs": [],
    }
    try:
        prepare_server_dir(spec, seed=args.seed)
        started = start_server(spec)
        wait_ready(spec, started)
        with RconClient(spec.rcon) as rcon:
            load_app(rcon)
            world_info = natural_world_info(rcon)
            summary["world"] = world_info
        stop_server(spec, started)
        started = None
        snapshot_golden(spec)

        for mode in modes:
            restore_world(spec)
            started = start_server(spec)
            wait_ready(spec, started)
            log_path = LOG_DIR / f"g3-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
            if log_path.exists():
                log_path.unlink()
            run_result = asyncio.run(run_observation(spec, mode=mode, log_path=log_path, max_steps=args.max_steps))
            run_result["secret_grep_count"] = grep_secret(log_path)
            if run_result["secret_grep_count"]:
                raise RuntimeError(f"secret token leak detected in {log_path}")
            summary["runs"].append(run_result)
            stop_server(spec, started)
            started = None
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
        return 0
    except AppConfigError as exc:
        summary["error"] = {"type": type(exc).__name__, "message": str(exc)}
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if started is not None:
            stop_server(spec, started)
        if not args.keep_server_dir:
            shutil.rmtree(spec.root, ignore_errors=True)


def prepare_server_dir(spec: ServerSpec, *, seed: str) -> None:
    if spec.root.exists():
        shutil.rmtree(spec.root)
    spec.root.mkdir(parents=True)
    for name in (
        "fabric-server-launch.jar",
        "fabric-server-launcher.properties",
        "server.jar",
        "libraries",
        "versions",
        "mods",
        "config",
        "carpet.conf",
        "eula.txt",
    ):
        src = TEMPLATE / name
        dst = spec.root / name
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    (spec.root / "logs").mkdir(exist_ok=True)
    world_scripts = spec.world / "scripts"
    world_scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "minecraft" / "server" / "scarpet" / "minebot.sc", world_scripts / "minebot.sc")
    (spec.root / "server.properties").write_text(server_properties(spec, seed=seed), encoding="utf-8")


def server_properties(spec: ServerSpec, *, seed: str) -> str:
    return "\n".join(
        [
            "accepts-transfers=false",
            "allow-flight=true",
            "broadcast-console-to-ops=false",
            "broadcast-rcon-to-ops=false",
            "difficulty=normal",
            "enable-command-block=false",
            "enable-jmx-monitoring=false",
            "enable-query=false",
            "enable-rcon=true",
            "enable-status=true",
            "enforce-secure-profile=true",
            "enforce-whitelist=false",
            "force-gamemode=false",
            "function-permission-level=2",
            "gamemode=survival",
            "generate-structures=true",
            "generator-settings={}",
            "hardcore=false",
            "hide-online-players=false",
            "initial-disabled-packs=",
            "initial-enabled-packs=vanilla",
            "level-name=world",
            f"level-seed={seed}",
            "level-type=minecraft\\:normal",
            "max-chained-neighbor-updates=1000000",
            "max-players=20",
            "max-tick-time=-1",
            "max-world-size=29999984",
            "motd=MineBot G3 Natural Observation",
            "network-compression-threshold=256",
            "online-mode=false",
            "op-permission-level=4",
            "pause-when-empty-seconds=-1",
            "player-idle-timeout=0",
            "prevent-proxy-connections=false",
            f"query.port={spec.server_port}",
            "rate-limit=0",
            f"rcon.password={spec.password}",
            f"rcon.port={spec.rcon_port}",
            "region-file-compression=deflate",
            "require-resource-pack=false",
            "resource-pack=",
            "resource-pack-id=",
            "resource-pack-prompt=",
            "resource-pack-sha1=",
            "server-ip=",
            f"server-port={spec.server_port}",
            "simulation-distance=6",
            "spawn-protection=0",
            "sync-chunk-writes=true",
            "text-filtering-config=",
            "text-filtering-version=0",
            "use-native-transport=true",
            "view-distance=6",
            "white-list=false",
            "",
        ]
    )


def start_server(spec: ServerSpec) -> subprocess.Popen[str]:
    spec.process_log.parent.mkdir(parents=True, exist_ok=True)
    fh = spec.process_log.open("a", encoding="utf-8")
    return subprocess.Popen(
        ["java", "-Xmx2G", "-jar", "fabric-server-launch.jar", "nogui"],
        cwd=spec.root,
        stdin=subprocess.DEVNULL,
        stdout=fh,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def wait_ready(spec: ServerSpec, proc: subprocess.Popen[str], *, timeout_s: float = 240.0) -> None:
    deadline = time.monotonic() + timeout_s
    saw_done = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited during startup: {proc.returncode}")
        if spec.latest_log.exists() and "Done (" in spec.latest_log.read_text(encoding="utf-8", errors="replace"):
            saw_done = True
        if saw_done:
            try:
                with RconClient(RconConfig(spec.host, spec.rcon_port, spec.password, timeout_s=3.0, reconnect_attempts=0)) as rcon:
                    rcon.command("list")
                return
            except Exception:
                pass
        time.sleep(1.0)
    raise TimeoutError(f"server did not become ready within {timeout_s}s")


def stop_server(spec: ServerSpec, proc: subprocess.Popen[str], *, timeout_s: float = 90.0) -> None:
    try:
        with RconClient(RconConfig(spec.host, spec.rcon_port, spec.password, timeout_s=3.0, reconnect_attempts=0)) as rcon:
            rcon.command("stop")
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10.0)


def load_app(rcon: RconClient) -> None:
    for command in (
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        "gamerule advance_time false",
        "gamerule advance_weather false",
        "gamerule spawn_mobs false",
        "gamerule random_tick_speed 0",
        "time set day",
        "weather clear",
        "difficulty normal",
        "script load minebot global",
    ):
        rcon.command(command)
        time.sleep(0.05)
    reset = rcon.command("script in minebot run minebot_reset()")
    if "true" not in reset.lower():
        raise RuntimeError(f"minebot_reset failed: {reset[:500]}")
    for entity_type in HOSTILE_TYPES:
        rcon.command(f"kill @e[type=minecraft:{entity_type}]")


def natural_world_info(rcon: RconClient) -> dict[str, object]:
    return {
        "difficulty": rcon.command("difficulty").strip(),
        "spawn_mobs": rcon.command("gamerule spawn_mobs").strip(),
        "advance_time": rcon.command("gamerule advance_time").strip(),
        "advance_weather": rcon.command("gamerule advance_weather").strip(),
        "random_tick_speed": rcon.command("gamerule random_tick_speed").strip(),
    }


def snapshot_golden(spec: ServerSpec) -> None:
    if spec.golden.exists():
        shutil.rmtree(spec.golden)
    shutil.copytree(spec.world, spec.golden, symlinks=True)


def restore_world(spec: ServerSpec) -> None:
    if spec.world.exists():
        shutil.rmtree(spec.world)
    shutil.copytree(spec.golden, spec.world, symlinks=True)


async def run_observation(spec: ServerSpec, *, mode: str, log_path: Path, max_steps: int) -> dict[str, object]:
    provider = provider_registry_from_env()
    with RconClient(spec.rcon) as rcon:
        load_app(rcon)
        body = ScarpetBody(BOT, rcon)
        prepare_bot(rcon, body)
        sink = JsonlObservationSink(log_path)
        language = agent_language_from_env(os.environ, default="Chinese")

        def make_parts(goal_text: str):
            trace = RuntimeTrace(session_id=f"g3-{mode}", sink=sink)
            if provider is not None:
                trace.emit(
                    "provider_manifest",
                    default_route=provider.default,
                    language=language,
                    providers=provider.trace_configs(),
                )
            return build_phase1_agent_runtime(
                body=body,
                goal_text=goal_text,
                model_provider=provider,
                config=Phase1RuntimeConfig(
                    natural_region=REGION,
                    recovery_respawn_pos=None,
                    recovery_gamemode="survival",
                ),
                agent_name=f"MineBotG3Natural-{mode}",
                language=language,
                trace=trace,
            )

        session = AgentSession(make_parts)
        session.submit(SessionCommand.start(GOAL))
        try:
            final = await session.run_until_waiting(
                max_steps=max_steps,
                should_stop=lambda step: safe_evaluate_terminal_truth(body, GOAL, step, session=session).satisfied,
            )
            truth = safe_evaluate_terminal_truth(body, GOAL, final, session=session)
            if truth.satisfied:
                final = session.complete_current_goal("terminal_truth_satisfied")
                truth = safe_evaluate_terminal_truth(body, GOAL, final, session=session)
            if session.parts is None:
                raise RuntimeError("session did not create runtime parts")
            session.parts.runtime.trace.emit(
                "session_terminal",
                mode=mode,
                status=final.status,
                lifecycle=final.lifecycle.value,
                message=final.message,
                terminal_truth=truth.to_trace(),
                observability=body.observability_snapshot(max_events=32, max_traces=16, max_requests=16),
            )
            session.parts.runtime.trace.close()
            events = read_jsonl(log_path)
            return {
                "mode": mode,
                "log": str(log_path),
                "status": final.status,
                "lifecycle": final.lifecycle.value,
                "message": final.message,
                "exit_code": truth.exit_code,
                "satisfied": truth.satisfied,
                "inventory_count": truth.inventory_count,
                "event_count": len(events),
                "model_event_count": sum(1 for event in events if is_model_event(event)),
                "tool_sequence": tool_sequence(events),
                "terminal_event": first_terminal_event(events),
            }
        finally:
            if provider is not None:
                await provider.aclose()


def prepare_bot(rcon: RconClient, body: ScarpetBody) -> None:
    rcon.command(f"player {BOT} kill")
    body.spawn(timeout_s=30.0, gamemode="survival")
    time.sleep(0.2)
    rcon.command(f"gamemode survival {BOT}")
    rcon.command(f"effect clear {BOT}")
    rcon.command(f"clear {BOT}")
    for slot in range(46):
        body.transport.request(f"script in minebot run inventory_set('{BOT}', {slot}, 0)")
    rcon.command("script in minebot run minebot_reset()")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    if not path.exists():
        return events
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                events.append(json.loads(line))
    return events


def is_model_event(event: dict[str, object]) -> bool:
    name = str(event.get("event") or "")
    return name.startswith("model_") or name in {
        "agent_start",
        "agent_end",
        "llm_start",
        "llm_end",
        "turn_completed",
        "assistant_message",
        "assistant_final_output",
        "model_tool_call",
        "model_message",
    }


def tool_sequence(events: list[dict[str, object]]) -> list[str]:
    sequence: list[str] = []
    for event in events:
        name = str(event.get("event") or "")
        if name in {"tool_invoke", "tool_result", "composition_tool_result"}:
            tool = event.get("tool")
            reason = event.get("reason")
            if reason:
                sequence.append(f"{name}:{tool}:{reason}")
            else:
                sequence.append(f"{name}:{tool}")
    return sequence


def first_terminal_event(events: list[dict[str, object]]) -> dict[str, object] | None:
    for event in events:
        if event.get("event") in {"session_terminal", "progress_yielded", "progress_aborted", "session_step_failed"}:
            return event
    return events[-1] if events else None


def grep_secret(path: Path) -> int:
    count = 0
    for name in ("ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "MINEBOT_LLM_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            count += path.read_text(encoding="utf-8", errors="replace").count(secret)
    return count


def _free_port(preferred: int) -> int:
    if _port_available(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


if __name__ == "__main__":
    raise SystemExit(main())
