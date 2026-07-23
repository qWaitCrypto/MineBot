"""Agent session entrypoint for an existing real Minecraft server.

Unlike the local console, this module must not prepare, reset, teleport, clear,
seed resources, or change gamerules. It only connects to an explicitly
configured real-server RCON endpoint and drives the Agent session.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from minebot.app.config import AppConfigError, agent_language_from_env, provider_registry_from_env
from minebot.app.body_events import BodyEventPump
from minebot.app.autonomy import AutonomyCoordinator
from minebot.app.conversation import PersistentWindowedConversationSession
from minebot.app.observation_artifacts import PersistentToolObservationArchive
from minebot.app.progress_epochs import PersistentProgressEpochArchive
from minebot.app.exploration import PersistentExplorationCoverageStore
from minebot.app.observability import JsonlObservationSink
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime, inventory_count
from minebot.app.memory import MemoryWorkspace
from minebot.app.skills import (
    SkillCatalog,
    SkillCatalogError,
    SkillOperationError,
    SkillWorkspace,
)
from minebot.app.wiki import WikiKnowledge
from minebot.app.reconciliation import StartupReconciliationError, enqueue_startup_reconciliation
from minebot.app.runtime_identity import RuntimeIdentityError, resolve_runtime_scope
from minebot.app.runtime_state import DEFAULT_RUNTIME_STATE_DB, RuntimeStateError, RuntimeStateStore
from minebot.app.runtime_state import TaskStatus
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import PersistentWorkIntentQueue
from minebot.app.runner import RuntimeTrace
from minebot.app.session import DEFAULT_RUNAWAY_STEP_LIMIT, AgentSession, SessionCommand, SessionCommandKind, SessionStep
from minebot.brain.lifecycle import LifecycleState
from minebot.brain.composition import resource_plan_for
from minebot.contract import Body, InventorySlot, Region
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import EnvelopeError, RconError
from minebot.game.protocol import build_state_call, build_watch_call, parse_state
from minebot.game.rcon import RconConfig

IDLE_BODY_STATE_SAMPLE_INTERVAL_S = 120.0


@dataclass(frozen=True)
class RealServerConfig:
    rcon: RconConfig
    bot_name: str
    natural_region: Region
    recovery_respawn_pos: tuple[int, int, int] | None
    log_path: Path
    language: str
    server_id: str
    world_id_override: str | None
    state_db_path: Path


class RealServerConfigError(RuntimeError):
    pass


_MINECRAFT_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,16}")

# This is the frozen primary material objective from the FakePlayer
# generalization corpus.  It is deliberately kept here as a literal so the
# production ingress and terminal evaluator share one exact contract without
# importing test-only fixtures.
AG_FP30_GOAL_ID = "AG-FP30"
AG_FP30_GOAL = (
    "Start empty-handed in the natural world. Collect 3 flower types, kill a pig, "
    "cow and sheep and keep their drops, craft and equip a shield and an iron pickaxe, "
    "keep at least 16 torches, and finish with at least 3 iron ingots."
)
AG_FP30_ACCEPTED_FLOWERS = frozenset(
    {
        "dandelion",
        "poppy",
        "blue_orchid",
        "allium",
        "azure_bluet",
        "red_tulip",
        "orange_tulip",
        "white_tulip",
        "pink_tulip",
        "oxeye_daisy",
        "cornflower",
        "lily_of_the_valley",
        "wither_rose",
        "sunflower",
        "lilac",
        "rose_bush",
        "peony",
        "torchflower",
        "pitcher_plant",
        "open_eyeblossom",
        "closed_eyeblossom",
    }
)
AG_FP30_DROP_FAMILIES = {
    "pig": frozenset({"porkchop"}),
    "cow": frozenset({"beef", "leather"}),
    "sheep": frozenset({"mutton", "white_wool", "wool"}),
}


@dataclass(frozen=True)
class InteractiveScenarioContext:
    """Restricted test-fixture surface for a production interactive session.

    The scenario owns only scheduled external facts. It cannot submit
    SessionCommands, inspect the tool registry, or invoke Body transactions.
    Every operation uses the session's existing RCON client.
    """

    bot_name: str
    _rcon: RconClient
    _trace_event: Callable[[str, Mapping[str, object]], None]
    _trace_snapshot: Callable[[], list[dict[str, object]]] = lambda: []

    async def emit_chat(self, sender: str, message: str) -> int:
        _require_minecraft_name(sender)
        text = str(message).strip()
        if not text:
            raise ValueError("scenario chat message must not be empty")
        command = (
            "script in minebot run emit_agent_chat("
            f"'{self.bot_name}', '{sender}', '{_escape_scarpet_string(text)}')"
        )
        await asyncio.to_thread(self._rcon.request, command)
        self._trace_event("scenario_chat_emitted", {"sender": sender, "message": text})
        records = self._trace_snapshot()
        return max(
            (int(record.get("seq") or 0) for record in records),
            default=0,
        )

    async def wait_for_idle_quiescence(
        self,
        *,
        after_trace_seq: int,
        timeout_s: float = 120.0,
    ) -> None:
        if after_trace_seq < 0:
            raise ValueError("idle marker sequence must not be negative")
        if timeout_s <= 0:
            raise ValueError("idle quiescence timeout must be positive")
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            for record in reversed(self._trace_snapshot()):
                if int(record.get("seq") or 0) <= after_trace_seq:
                    break
                if (
                    record.get("event") == "autonomy_decision"
                    and record.get("action") == "park"
                    and record.get("reason") == "checkpoint_wait_event"
                ):
                    self._trace_event(
                        "scenario_idle_quiescent",
                        {"after_trace_seq": after_trace_seq},
                    )
                    return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("agent did not enter checkpoint_wait_event quiescence")
            await asyncio.sleep(0.25)

    async def wait_for_body_ready(self, *, timeout_s: float = 60.0) -> None:
        if timeout_s <= 0:
            raise ValueError("fixture ready timeout must be positive")
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            state = await self._read_bot_state()
            if not state.missing:
                self._trace_event("scenario_body_ready", {"position": list(state.pos)})
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("fixture Bot did not become ready before timeout")
            await asyncio.sleep(0.25)

    async def spawn_fake_player(
        self,
        name: str,
        position: tuple[int, int, int],
    ) -> None:
        _require_minecraft_name(name)
        x, y, z = _fixture_position(position)
        await asyncio.to_thread(self._rcon.request, f"player {name} kill")
        await asyncio.to_thread(self._rcon.request, f"player {name} spawn at {x} {y} {z}")
        self._trace_event(
            "scenario_fake_player_spawned",
            {"name": name, "position": [x, y, z]},
        )

    async def remove_fake_player(self, name: str) -> None:
        _require_minecraft_name(name)
        await asyncio.to_thread(self._rcon.request, f"player {name} kill")
        self._trace_event("scenario_fake_player_removed", {"name": name})

    async def spawn_fake_player_near_bot(self, name: str, *, distance: int = 5) -> None:
        if distance < 1 or distance > 32:
            raise ValueError("fixture fake-player distance must be between 1 and 32")
        state = await self._bot_state()
        await self.spawn_fake_player(
            name,
            (round(state.pos[0]) + distance, round(state.pos[1]), round(state.pos[2])),
        )

    async def set_difficulty(self, difficulty: str) -> None:
        normalized = str(difficulty).lower()
        if normalized not in {"peaceful", "easy", "normal", "hard"}:
            raise ValueError(f"unsupported fixture difficulty: {difficulty!r}")
        await asyncio.to_thread(self._rcon.request, f"difficulty {normalized}")
        self._trace_event("scenario_difficulty_set", {"difficulty": normalized})

    async def spawn_husk_near_bot(
        self,
        *,
        distance: int = 2,
        offset: tuple[int, int] | None = None,
    ) -> None:
        if distance < 1 or distance > 8:
            raise ValueError("fixture hostile distance must be between 1 and 8")
        state = await self._bot_state()
        dx, dz = (distance, 0) if offset is None else offset
        if dx == 0 and dz == 0 or abs(dx) > 8 or abs(dz) > 8:
            raise ValueError("fixture hostile offset must be non-zero and within 8 blocks")
        x = round(state.pos[0]) + dx
        y = round(state.pos[1])
        z = round(state.pos[2]) + dz
        await asyncio.to_thread(self._rcon.request, "kill @e[type=minecraft:husk]")
        await asyncio.to_thread(
            self._rcon.request,
            f"summon husk {x} {y} {z} {{PersistenceRequired:1b}}",
        )
        self._trace_event(
            "scenario_husk_spawned",
            {"position": [x, y, z], "distance": distance},
        )

    async def provoke_husk_attack(self, *, timeout_s: float = 45.0) -> None:
        if timeout_s <= 0:
            raise ValueError("fixture attack timeout must be positive")
        baseline = await self._bot_state()
        baseline_health = baseline.health
        deadline = asyncio.get_running_loop().time() + timeout_s
        offsets = ((1, 0), (-1, 0), (0, 1), (0, -1))
        attempt = 0
        while asyncio.get_running_loop().time() < deadline:
            await self.spawn_husk_near_bot(offset=offsets[attempt % len(offsets)])
            attempt += 1
            attempt_deadline = min(deadline, asyncio.get_running_loop().time() + 8.0)
            while asyncio.get_running_loop().time() < attempt_deadline:
                state = await self._bot_state()
                if state.health < baseline_health:
                    self._trace_event(
                        "scenario_husk_attack_observed",
                        {"health": state.health, "attempt": attempt},
                    )
                    return
                await asyncio.sleep(0.25)
            await self.clear_hostiles()
        raise TimeoutError("fixture husk did not damage the bot")

    async def clear_hostiles(self) -> None:
        await asyncio.to_thread(self._rcon.request, "kill @e[type=minecraft:husk]")
        self._trace_event("scenario_hostiles_cleared", {})

    async def _bot_state(self):
        state = await self._read_bot_state()
        if state.missing:
            raise RuntimeError("cannot schedule fixture fact while Bot is missing")
        return state

    async def _read_bot_state(self):
        state = await asyncio.to_thread(
            parse_state,
            self._rcon.request(build_state_call(self.bot_name)),
        )
        return state


InteractiveScenarioHook = Callable[[InteractiveScenarioContext], Awaitable[None]]


def _require_minecraft_name(name: str) -> None:
    if _MINECRAFT_NAME_RE.fullmatch(name) is None:
        raise ValueError("fixture Minecraft names must be 1-16 alphanumeric or underscore characters")


def _fixture_position(position: tuple[int, int, int]) -> tuple[int, int, int]:
    if len(position) != 3:
        raise ValueError("fixture position must contain exactly three coordinates")
    values = tuple(int(value) for value in position)
    if any(abs(value) > 30_000_000 for value in values):
        raise ValueError("fixture position is outside Minecraft world bounds")
    return values


def _escape_scarpet_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


@dataclass
class _CameraSession:
    config_path: Path | None
    launched: bool = False
    owned: bool = False
    monitor_task: asyncio.Task[None] | None = None
    ready_reported: bool = False
    failure: str | None = None
    last_state: dict[str, object] | None = None

    def maybe_start(self, body: Body) -> None:
        if self.config_path is None or self.launched:
            return
        try:
            state = body.get_state()
        except (EnvelopeError, RconError, ValueError):
            return
        if state.missing:
            return

        self.launched = True
        from minebot.camera.config import CameraConfigError
        from minebot.camera.service import CameraServiceError, start_service

        try:
            camera_state = start_service(
                self.config_path,
                force=True,
                wait_for_ready=False,
            )
        except (CameraConfigError, CameraServiceError) as exc:
            self.failure = str(exc)
            print(f"Camera unavailable; continuing without it: {self.failure}", file=sys.stderr)
            return

        self.owned = camera_state.get("started") is True
        if camera_state.get("phase") == "ready":
            _print_camera_ready(camera_state)
            self.ready_reported = True
        else:
            print(f"Camera starting: target={camera_state.get('target')}", flush=True)
        self.last_state = dict(camera_state)
        self.monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self) -> None:
        assert self.config_path is not None
        from minebot.camera.config import CameraConfigError
        from minebot.camera.service import CameraServiceError, service_status

        while True:
            try:
                state = service_status(self.config_path)
            except (CameraConfigError, CameraServiceError) as exc:
                self.failure = str(exc)
                print(f"Camera unavailable; continuing without it: {self.failure}", file=sys.stderr)
                return
            self.last_state = dict(state)
            phase = state.get("phase")
            if phase == "ready":
                if not self.ready_reported:
                    _print_camera_ready(state)
                    self.ready_reported = True
            if phase in {"failed", "stopped"}:
                detail = state.get("error") or f"Camera entered {phase}"
                self.failure = str(detail)
                print(f"Camera unavailable; continuing without it: {self.failure}", file=sys.stderr)
                return
            await asyncio.sleep(0.25)

    async def close(self) -> None:
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor_task
        if not self.owned or self.config_path is None:
            return
        from minebot.camera.config import CameraConfigError
        from minebot.camera.service import CameraServiceError, stop_service

        try:
            state = stop_service(self.config_path)
        except (CameraConfigError, CameraServiceError) as exc:
            print(f"Camera cleanup warning: {exc}", file=sys.stderr)
            return
        self.last_state = dict(state)
        error = state.get("error")
        if error:
            self.failure = str(error)
            print(f"Camera cleanup warning: {self.failure}", file=sys.stderr)


def _print_camera_ready(state: Mapping[str, object]) -> None:
    print(
        "Camera ready:"
        f" target={state.get('target')}"
        f" record={'on' if state.get('recording') else 'off'}"
        f" live={'on' if state.get('live') else 'off'}",
        flush=True,
    )


@dataclass(frozen=True)
class CollectTarget:
    item: str
    count: int
    inventory_items: tuple[str, ...]


@dataclass(frozen=True)
class GoalTarget:
    kind: str
    item: str
    count: int
    inventory_items: tuple[str, ...]


@dataclass(frozen=True)
class CompositeGoalTarget:
    kind: str
    goal_id: str


@dataclass(frozen=True)
class TerminalTruth:
    goal: str
    target: GoalTarget | CollectTarget | CompositeGoalTarget | None
    inventory_count: int | None
    satisfied: bool
    status: str
    lifecycle: str
    exit_code: int
    facts: dict[str, object] = field(default_factory=dict)

    def to_trace(self) -> dict[str, object]:
        target_payload: dict[str, object] | None = None
        if self.target is not None:
            if isinstance(self.target, CompositeGoalTarget):
                target_payload = {
                    "kind": self.target.kind,
                    "goal_id": self.target.goal_id,
                }
            else:
                target_payload = {
                    "kind": getattr(self.target, "kind", "collect"),
                    "item": self.target.item,
                    "count": self.target.count,
                    "inventory_items": list(self.target.inventory_items),
                }
        return {
            "goal": self.goal,
            "target": target_payload,
            "inventory_count": self.inventory_count,
            "satisfied": self.satisfied,
            "status": self.status,
            "lifecycle": self.lifecycle,
            "exit_code": self.exit_code,
            "facts": dict(self.facts),
        }


def env_required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise RealServerConfigError(f"missing required env var {name}")
    return value


def real_server_config_from_env(env: Mapping[str, str] | None = None) -> RealServerConfig:
    env = os.environ if env is None else env
    host = env_required(env, "MINEBOT_REAL_RCON_HOST")
    port = int(env_required(env, "MINEBOT_REAL_RCON_PORT"))
    password = env_required(env, "MINEBOT_REAL_RCON_PASSWORD")
    bot_name = env_required(env, "MINEBOT_REAL_BOT")
    timeout_s = float(env.get("MINEBOT_REAL_RCON_TIMEOUT", "20"))
    natural_region = _region_from_env(env)
    recovery_respawn_pos = _position_from_env(env, "MINEBOT_REAL_RECOVERY_RESPAWN_POS")
    log_path = Path(env.get("MINEBOT_AGENT_LOG_PATH") or "logs/agent-session.jsonl")
    language = agent_language_from_env(env)
    server_id = (env.get("MINEBOT_REAL_SERVER_ID") or f"{host}:{port}").strip()
    world_id_override = (env.get("MINEBOT_REAL_WORLD_ID") or "").strip() or None
    state_db_path = Path(env.get("MINEBOT_AGENT_STATE_DB") or DEFAULT_RUNTIME_STATE_DB)
    return RealServerConfig(
        rcon=RconConfig(host=host, port=port, password=password, timeout_s=timeout_s),
        bot_name=bot_name,
        natural_region=natural_region,
        recovery_respawn_pos=recovery_respawn_pos,
        log_path=log_path,
        language=language,
        server_id=server_id,
        world_id_override=world_id_override,
        state_db_path=state_db_path,
    )


def _region_from_env(env: Mapping[str, str]) -> Region:
    raw = env.get("MINEBOT_REAL_NATURAL_REGION")
    if raw:
        parts = [int(part.strip()) for part in raw.split(",")]
        if len(parts) != 6:
            raise RealServerConfigError("MINEBOT_REAL_NATURAL_REGION must be six comma-separated ints")
        return Region("real-server-natural", tuple(parts[:3]), tuple(parts[3:]))
    return Region("real-server-natural", (-256, -64, -256), (256, 320, 256))


def _position_from_env(env: Mapping[str, str], name: str) -> tuple[int, int, int] | None:
    raw = env.get(name)
    if not raw:
        return None
    parts = [int(part.strip()) for part in raw.split(",")]
    if len(parts) != 3:
        raise RealServerConfigError(f"{name} must be three comma-separated ints")
    return tuple(parts)


async def run_real_server_goal(
    config: RealServerConfig,
    goal: str,
    *,
    max_steps: int | None,
    camera_config: Path | None = None,
) -> int:
    provider = provider_registry_from_env()
    rcon = RconClient(config.rcon)
    try:
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        print(
            f"Real-server RCON unavailable at {config.rcon.host}:{config.rcon.port}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        await provider.aclose()
        return 3

    with rcon:
        camera = _CameraSession(camera_config)
        try:
            _ensure_scarpet_global_app(rcon, config.bot_name)
        except (EnvelopeError, RconError) as exc:
            print(
                f"Real-server Scarpet app unavailable at {config.rcon.host}:{config.rcon.port}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            await provider.aclose()
            return 4
        body = ScarpetBody(config.bot_name, rcon)
        camera.maybe_start(body)
        sink = JsonlObservationSink(config.log_path)

        def make_parts(goal_text: str):
            trace = RuntimeTrace(session_id=config.bot_name, sink=sink)
            trace.emit(
                "provider_manifest",
                default_route=provider.default,
                language=config.language,
                providers=provider.trace_configs(),
            )
            parts = build_phase1_agent_runtime(
                body=body,
                goal_text=goal_text,
                model_provider=provider,
                config=Phase1RuntimeConfig(
                    natural_region=config.natural_region,
                    recovery_respawn_pos=config.recovery_respawn_pos,
                    recovery_gamemode="survival",
                ),
                agent_name="MineBotRealServer",
                language=config.language,
                trace=trace,
            )
            return parts

        session = AgentSession(make_parts)
        session.submit(SessionCommand.start(goal))
        try:
            def should_stop(step: SessionStep) -> bool:
                camera.maybe_start(body)
                return safe_evaluate_terminal_truth(body, goal, step, session=session).satisfied

            final = await session.run_until_waiting(
                max_steps=max_steps,
                should_stop=should_stop,
            )
            terminal_goal = _session_goal(session, goal)
            truth = safe_evaluate_terminal_truth(body, terminal_goal, final, session=session)
            if truth.satisfied:
                final = session.complete_current_goal("terminal_truth_satisfied")
                truth = safe_evaluate_terminal_truth(body, terminal_goal, final, session=session)
            if session.parts is not None:
                session.parts.runtime.trace.emit(
                    "session_terminal",
                    status=final.status,
                    lifecycle=final.lifecycle.value,
                    message=final.message,
                    terminal_truth=truth.to_trace(),
                )
                session.parts.runtime.trace.close()
            print(f"log={config.log_path}")
            print(
                f"status={final.status} lifecycle={final.lifecycle.value} "
                f"satisfied={truth.satisfied} inventory_count={truth.inventory_count}"
            )
            return truth.exit_code
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
            await camera.close()
            await provider.aclose()


async def run_real_server_interactive(
    config: RealServerConfig,
    goal: str | None,
    *,
    max_steps: int | None,
    camera_config: Path | None = None,
    scenario_hook: InteractiveScenarioHook | None = None,
    terminal_goal: str | None = None,
) -> int:
    """Run one persistent real-server session with stdin as the user channel.

    ``terminal_goal`` is an optional evaluator contract for production
    scenarios that inject/replace goals through the live ingress.  It is never
    submitted to the Agent automatically; it only keeps final verification
    anchored after cancellation or ``/quit`` clears the active goal.
    """
    provider = provider_registry_from_env()
    rcon = RconClient(config.rcon)
    try:
        rcon.connect()
    except (OSError, PermissionError, RconError) as exc:
        print(
            f"Real-server RCON unavailable at {config.rcon.host}:{config.rcon.port}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        await provider.aclose()
        return 3

    with rcon:
        camera = _CameraSession(camera_config)
        state_store: RuntimeStateStore | None = None
        conversation_session: PersistentWindowedConversationSession | None = None
        work_queue: PersistentWorkIntentQueue | None = None
        body = ScarpetBody(config.bot_name, rcon)
        try:
            app_reloaded = _ensure_scarpet_global_app(rcon, config.bot_name)
            _watch_interactive_chat(rcon, config.bot_name)
            scope = resolve_runtime_scope(
                rcon,
                server_id=config.server_id,
                bot_id=config.bot_name,
                world_id_override=config.world_id_override,
            )
            state_store = RuntimeStateStore(config.state_db_path)
            state_store.register_scope(scope)
            task_workspace = TaskWorkspace(state_store, scope)
            memory_workspace = MemoryWorkspace(state_store, scope)
            skill_workspace = SkillWorkspace(
                state_store,
                scope,
                SkillCatalog(),
                task_workspace=task_workspace,
            )
            wiki_knowledge = WikiKnowledge(state_store)
            work_queue = PersistentWorkIntentQueue(state_store, scope)
            observation_archive = PersistentToolObservationArchive(state_store, scope)
            progress_epoch_archive = PersistentProgressEpochArchive(state_store, scope)
            exploration_coverage_store = PersistentExplorationCoverageStore(state_store, scope)
            conversation_session = PersistentWindowedConversationSession(
                scope.conversation_session_id,
                config.state_db_path,
                archive_store=state_store,
                scope=scope,
            )
            await conversation_session.sync_archive()
            body_event_pump = BodyEventPump(
                body,
                work_queue,
                state_store,
                scope,
            )
            startup_reconciliation = enqueue_startup_reconciliation(
                body=body,
                event_pump=body_event_pump,
                queue=work_queue,
                workspace=task_workspace,
                orphaned_intents=work_queue.orphaned_intents,
                app_reloaded=app_reloaded,
                terminal_probe=_startup_terminal_probe,
            )
        except (
            EnvelopeError,
            RconError,
            RuntimeIdentityError,
            RuntimeStateError,
            SkillCatalogError,
            SkillOperationError,
            StartupReconciliationError,
            ValueError,
        ) as exc:
            if work_queue is not None:
                work_queue.close()
            if conversation_session is not None:
                conversation_session.close()
            if state_store is not None:
                state_store.close()
            print(
                f"Real-server runtime unavailable at {config.rcon.host}:{config.rcon.port}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            await provider.aclose()
            return 4
        sink = JsonlObservationSink(config.log_path)
        speech_sink = _interactive_speech_sink(body)

        def make_parts(goal_text: str):
            trace = RuntimeTrace(session_id=config.bot_name, sink=sink)
            trace.emit(
                "provider_manifest",
                default_route=provider.default,
                language=config.language,
                providers=provider.trace_configs(),
            )
            parts = build_phase1_agent_runtime(
                body=body,
                goal_text=goal_text,
                model_provider=provider,
                config=Phase1RuntimeConfig(
                    natural_region=config.natural_region,
                    recovery_respawn_pos=config.recovery_respawn_pos,
                    recovery_gamemode="survival",
                    speech_sink=speech_sink,
                    conversation_session=conversation_session,
                    task_workspace=task_workspace,
                    observation_archive=observation_archive,
                    progress_epoch_archive=progress_epoch_archive,
                    exploration_coverage_store=exploration_coverage_store,
                    memory_workspace=memory_workspace,
                    skill_workspace=skill_workspace,
                    wiki_knowledge=wiki_knowledge,
                ),
                agent_name="MineBotRealServer",
                language=config.language,
                trace=trace,
            )
            trace.emit("runtime_scope", **scope.to_payload(), scope_key=scope.key)
            trace.emit(
                "startup_reconciliation",
                intent_id=startup_reconciliation.intent.intent_id,
                decision=startup_reconciliation.decision.value,
                event_count=len(startup_reconciliation.events),
                orphaned_intent_count=len(startup_reconciliation.orphaned_intents),
                inventory_counts=startup_reconciliation.inventory_counts,
                state_missing=startup_reconciliation.state.missing,
                state_pos=list(startup_reconciliation.state.pos),
                app_reloaded=app_reloaded,
            )
            return parts

        session = AgentSession(
            make_parts,
            task_workspace=task_workspace,
            skill_workspace=skill_workspace,
            work_queue=work_queue,
            autonomy_coordinator=AutonomyCoordinator(
                task_workspace,
                work_queue,
                progress_epoch_archive,
            ),
        )
        if goal:
            if task_workspace.current_task is None:
                session.submit(SessionCommand.start(goal))
            else:
                session.submit(
                    SessionCommand.replace_goal(
                        goal,
                        reason="startup_goal_replaced_persisted_task",
                    )
                )
        reader = asyncio.create_task(_stdin_command_reader(session))
        chat_reader = asyncio.create_task(_chat_command_reader(session, body_event_pump))
        scenario_task: asyncio.Task[None] | None = None
        if scenario_hook is not None:
            def trace_scenario_event(event: str, fields: Mapping[str, object]) -> None:
                parts = session.parts
                if parts is not None:
                    parts.runtime.trace.emit(event, **dict(fields))

            scenario_context = InteractiveScenarioContext(
                bot_name=config.bot_name,
                _rcon=rcon,
                _trace_event=trace_scenario_event,
                _trace_snapshot=lambda: (
                    []
                    if session.parts is None
                    else session.parts.runtime.trace.snapshot()
                ),
            )
            scenario_task = asyncio.create_task(scenario_hook(scenario_context))

            def record_scenario_failure(task: asyncio.Task[None]) -> None:
                if task.cancelled():
                    return
                try:
                    task.result()
                except Exception as exc:
                    trace_scenario_event(
                        "scenario_fixture_failed",
                        {"error_type": type(exc).__name__, "message": str(exc)},
                    )

            scenario_task.add_done_callback(record_scenario_failure)
        print(
            f"interactive_ready bot={config.bot_name} "
            f"server={config.rcon.host}:{config.rcon.port}",
            flush=True,
        )
        try:
            final = await _run_interactive_loop(
                session,
                fallback_goal=terminal_goal or goal,
                body=body,
                max_steps=max_steps,
                body_event_pump=body_event_pump,
                iteration_hook=lambda: camera.maybe_start(body),
            )
            if scenario_task is not None and scenario_task.done():
                scenario_task.result()
            evaluated_goal = _session_goal(session, terminal_goal or goal)
            truth = safe_evaluate_terminal_truth(body, evaluated_goal, final, session=session)
            _announce_interactive_terminal(body, truth)
            if session.parts is not None:
                session.parts.runtime.trace.emit(
                    "session_terminal",
                    mode="interactive",
                    status=final.status,
                    lifecycle=final.lifecycle.value,
                    message=final.message,
                    terminal_truth=truth.to_trace(),
                )
                session.parts.runtime.trace.close()
            print(f"log={config.log_path}")
            print(
                f"status={final.status} lifecycle={final.lifecycle.value} "
                f"satisfied={truth.satisfied} inventory_count={truth.inventory_count}"
            )
            return truth.exit_code
        finally:
            if scenario_task is not None:
                scenario_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await scenario_task
            reader.cancel()
            chat_reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            with contextlib.suppress(asyncio.CancelledError):
                await chat_reader
            close = getattr(session, "close", None)
            if callable(close):
                close()
            if session.parts is None:
                conversation_session.close()
            state_store.close()
            await camera.close()
            await provider.aclose()


def _ensure_scarpet_global_app(rcon: RconClient, bot_name: str) -> bool:
    command = build_state_call(bot_name)
    try:
        parse_state(rcon.request(command))
        return False
    except EnvelopeError:
        if re.fullmatch(r"[A-Za-z0-9_]{1,16}", bot_name) is None:
            raise ValueError("invalid Minecraft bot name")
        rcon.request(f"player {bot_name} stop")
        rcon.request("script load minebot global")
        parse_state(rcon.request(command))
        return True


def _startup_terminal_probe(task, counts: dict[str, int]) -> dict[str, object]:
    target = parse_goal_target(task.goal_text)
    if target is None:
        return {"satisfied": False, "target": None}
    observed = sum(
        counts.get(item.removeprefix("minecraft:"), 0)
        for item in target.inventory_items
    )
    return {
        "satisfied": observed >= target.count,
        "inventory_count": observed,
        "target": {
            "kind": target.kind,
            "item": target.item,
            "count": target.count,
            "inventory_items": list(target.inventory_items),
        },
    }


def _watch_interactive_chat(rcon: RconClient, bot_name: str) -> None:
    rcon.request(build_watch_call(bot_name))


def _interactive_speech_sink(body: object):
    last_text = {"value": None}
    say = getattr(body, "say", None)

    def sink(text: str) -> None:
        if not callable(say):
            return
        if text == last_text["value"]:
            return
        last_text["value"] = text
        say(text)

    return sink


def _announce_interactive_terminal(body: object, truth: TerminalTruth) -> bool:
    say = getattr(body, "say", None)
    if not callable(say):
        return False
    announcement = _terminal_announcement(truth)
    if not announcement:
        return False
    return bool(say(announcement))


def _terminal_announcement(truth: TerminalTruth) -> str | None:
    if truth.satisfied:
        if (
            truth.target is not None
            and not isinstance(truth.target, CompositeGoalTarget)
            and truth.inventory_count is not None
        ):
            return f"done: {truth.target.item} {truth.inventory_count}/{truth.target.count}"
        return "done"
    if truth.status == "yielded" or truth.lifecycle == "yielded":
        return "yielded: waiting for direction"
    if truth.status == "failed":
        return "failed: needs attention"
    return None


async def _run_interactive_loop(
    session: AgentSession,
    *,
    fallback_goal: str | None,
    body: Body,
    max_steps: int | None,
    chat_source: object | None = None,
    body_event_pump: BodyEventPump | None = None,
    iteration_hook: Callable[[], None] | None = None,
) -> SessionStep:
    last = None
    remaining = max_steps
    while remaining is None or remaining > 0:
        if iteration_hook is not None:
            iteration_hook()
        _poll_chat_commands(session, chat_source)
        if not getattr(session, "has_pending_work", True):
            if body_event_pump is not None:
                try:
                    task_workspace = getattr(session, "task_workspace", None)
                    task = None if task_workspace is None else task_workspace.current_task
                    task_waiting = task is not None and task.status is TaskStatus.WAITING_EVENT
                    checkpoint = (
                        None
                        if task is None or not task_waiting
                        else task_workspace.store.get_latest_checkpoint(task.task_id)
                    )
                    task_wakeable = task is not None and task.status in {
                        TaskStatus.RUNNING,
                        TaskStatus.WAITING_EVENT,
                    }
                    checkpoint_generation = None
                    if checkpoint is not None and checkpoint.body_fingerprint is not None:
                        raw_generation = checkpoint.body_fingerprint.get("generation")
                        if raw_generation is not None:
                            checkpoint_generation = int(raw_generation)
                    poll_result = await asyncio.to_thread(
                        body_event_pump.poll_once,
                        task_id=task.task_id if task_wakeable else None,
                        generation=checkpoint_generation,
                        task_waiting=task_waiting,
                        wait_checkpoint_id=(
                            checkpoint.checkpoint_id if checkpoint is not None else None
                        ),
                        wait_for=() if checkpoint is None else checkpoint.wait_for,
                    )
                except Exception as exc:
                    _trace_body_event_poll_failure(session, exc)
                else:
                    _trace_body_event_poll(session, poll_result)
                    _maybe_trace_idle_body_state(session, body)
            await asyncio.sleep(0.25 if body_event_pump is not None else 0.05)
            continue
        last = await session.step()
        if last.status == "quit":
            return last
        truth = safe_evaluate_terminal_truth(body, _session_goal(session, fallback_goal), last, session=session)
        if truth.satisfied:
            completed = session.complete_current_goal("terminal_truth_satisfied")
            completed_truth = safe_evaluate_terminal_truth(body, truth.goal, completed, session=session)
            _announce_interactive_terminal(body, completed_truth)
            last = completed
        elif (
            truth.target is None
            and getattr(session, "task_workspace", None) is not None
            and session.task_workspace.completion_requested
        ):
            parts = getattr(session, "parts", None)
            if parts is not None:
                checkpoint = session.task_workspace.store.get_latest_checkpoint(
                    session.task_workspace.current_task.task_id
                )
                parts.runtime.trace.emit(
                    "task_completion_pending_verification",
                    goal=truth.goal,
                    checkpoint_id=(
                        None if checkpoint is None else checkpoint.checkpoint_id
                    ),
                    evidence=[] if checkpoint is None else list(checkpoint.evidence),
                    required_authority="human_or_typed_verifier",
                )
        if remaining is not None:
            remaining -= 1
        await asyncio.sleep(0)
    assert last is not None
    return last


def _poll_chat_commands(session: AgentSession, chat_source: object | None) -> int:
    if chat_source is None:
        return 0
    poll = getattr(chat_source, "poll_chat_events", None)
    if not callable(poll):
        return 0
    try:
        events = poll()
    except Exception as exc:
        _trace_chat_poll_failure(session, exc)
        return 0
    count = _submit_chat_events(
        session,
        events,
        event_epoch=str(getattr(chat_source, "epoch", "") or "") or None,
    )
    acknowledge = getattr(chat_source, "acknowledge_cursor", None)
    if callable(acknowledge):
        acknowledge()
    return count


def _submit_chat_events(
    session: AgentSession,
    events: object,
    *,
    event_epoch: str | None = None,
) -> int:
    count = 0
    for event in events:
        if getattr(event, "name", None) != "agentChat":
            continue
        data = getattr(event, "data", {}) or {}
        message = str(data.get("message") or "").strip()
        if not message:
            continue
        command = parse_session_command(message)
        if command is not None and command.kind is SessionCommandKind.MESSAGE:
            promoted = parse_canonical_goal_command(
                message,
                has_active_goal=bool(getattr(session, "has_active_goal", False)),
            )
            if promoted is not None:
                command = promoted
            elif getattr(session, "parts", None) is None:
                command = SessionCommand.message(message, reason="chat_session_started")
        if command is None:
            continue
        sender = str(data.get("sender") or "")
        command = SessionCommand(
            kind=command.kind,
            text=command.text,
            reason=command.reason,
            sender=sender,
        )
        parts = getattr(session, "parts", None)
        if parts is not None:
            parts.runtime.trace.emit(
                "chat_message",
                sender=sender,
                command=command.kind.value,
                content=command.text,
                reason=command.reason,
            )
        seq = int(getattr(event, "seq", 0) or 0)
        dedupe_key = (
            None
            if event_epoch is None or seq <= 0
            else f"chat:{event_epoch}:{seq}"
        )
        if dedupe_key is None:
            session.submit(command)
        else:
            session.submit(command, dedupe_key=dedupe_key)
        count += 1
    return count


def _trace_chat_poll_failure(session: AgentSession, exc: Exception) -> None:
    parts = getattr(session, "parts", None)
    if parts is not None:
        parts.runtime.trace.emit("chat_poll_failed", error_type=type(exc).__name__)


def _trace_body_event_poll(session: AgentSession, result: object) -> None:
    if int(getattr(result, "observed", 0) or 0) <= 0:
        return
    parts = getattr(session, "parts", None)
    if parts is not None:
        parts.runtime.trace.emit(
            "idle_body_events_polled",
            observed=getattr(result, "observed", 0),
            material=getattr(result, "material", 0),
            enqueued=getattr(result, "enqueued", 0),
            last_seq=getattr(result, "last_seq", 0),
            epoch=getattr(result, "epoch", ""),
        )


def _maybe_trace_idle_body_state(
    session: AgentSession,
    body: Body,
    *,
    interval_s: float = IDLE_BODY_STATE_SAMPLE_INTERVAL_S,
) -> bool:
    parts = getattr(session, "parts", None)
    if parts is None:
        return False
    runtime = getattr(parts, "runtime", None)
    if runtime is None:
        return False
    now = time.monotonic()
    last = float(getattr(runtime, "_last_idle_body_state_trace_monotonic", 0.0) or 0.0)
    if last > 0.0 and now - last < interval_s:
        return False
    try:
        state = body.get_state()
    except Exception as exc:
        runtime.trace.emit(
            "idle_body_state_poll_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        setattr(runtime, "_last_idle_body_state_trace_monotonic", now)
        return False
    if getattr(state, "missing", False):
        runtime.trace.emit(
            "body_state",
            source="idle_body_state_poll",
            bot=getattr(state, "bot", None),
            pos=list(getattr(state, "pos", ())),
            health=getattr(state, "health", None),
            food=getattr(state, "food", None),
            oxygen=getattr(state, "oxygen", None),
            inventory_hash=getattr(state, "inventory_hash", None),
            inventory_counts=dict(getattr(state, "inventory_counts", None) or {}),
            selected_slot=getattr(state, "selected_slot", None),
            selected_item=getattr(state, "selected_item", None),
            offhand_item=getattr(state, "offhand_item", None),
            body_owner=getattr(state, "body_owner", None),
            pending_action_count=getattr(state, "pending_action_count", None),
            dimension=getattr(state, "dimension", None),
            complete=getattr(state, "complete", False),
            missing=True,
        )
        setattr(runtime, "_last_idle_body_state_trace_monotonic", now)
        return True
    runtime._remember_body_state(state)
    runtime._emit_last_known_body_state_trace(source="idle_body_state_poll")
    setattr(runtime, "_last_idle_body_state_trace_monotonic", now)
    return True


def _trace_body_event_poll_failure(session: AgentSession, exc: Exception) -> None:
    parts = getattr(session, "parts", None)
    if parts is not None:
        parts.runtime.trace.emit(
            "idle_body_event_poll_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


def _session_goal(session: AgentSession, fallback: str | None) -> str:
    current = getattr(session, "current_goal", None)
    if current:
        return current
    return fallback or ""


async def _stdin_command_reader(session: AgentSession) -> None:
    stopped = threading.Event()

    def read() -> None:
        while not stopped.is_set():
            line = sys.stdin.readline()
            if line == "":
                return
            command = parse_session_command(line)
            if command is not None:
                session.submit(command)

    threading.Thread(target=read, name="minebot-stdin", daemon=True).start()
    try:
        while True:
            await asyncio.sleep(0.25)
    finally:
        stopped.set()


async def _chat_command_reader(session: AgentSession, chat_source: object, *, poll_interval_s: float = 0.25) -> None:
    poll = getattr(chat_source, "poll_chat_events", None)
    if not callable(poll):
        return
    while True:
        try:
            events = await asyncio.to_thread(poll)
        except Exception as exc:
            _trace_chat_ingress_failure(session, phase="poll", exc=exc)
        else:
            try:
                await asyncio.to_thread(
                    _submit_chat_events,
                    session,
                    events,
                    event_epoch=str(getattr(chat_source, "epoch", "") or "") or None,
                )
            except Exception as exc:
                _trace_chat_ingress_failure(session, phase="submit", exc=exc)
            else:
                acknowledge = getattr(chat_source, "acknowledge_cursor", None)
                if callable(acknowledge):
                    try:
                        await asyncio.to_thread(acknowledge)
                    except Exception as exc:
                        _trace_chat_ingress_failure(session, phase="acknowledge", exc=exc)
        await asyncio.sleep(poll_interval_s)


def _trace_chat_ingress_failure(session: AgentSession, *, phase: str, exc: Exception) -> None:
    parts = getattr(session, "parts", None)
    if parts is not None:
        parts.runtime.trace.emit(
            "chat_ingress_failed",
            phase=phase,
            error_type=type(exc).__name__,
        )


def parse_session_command(line: str) -> SessionCommand | None:
    text = line.strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"/quit", "quit", "/exit", "exit"} or lowered.startswith(("/quit ", "/exit ")):
        tail = _command_tail(text, "/quit") if lowered.startswith("/quit") else _command_tail(text, "/exit")
        return SessionCommand.quit(tail or "user_quit")
    if lowered in {"/pause", "pause"} or lowered.startswith("/pause "):
        return SessionCommand.pause(_command_tail(text, "/pause") or "user_pause")
    if lowered in {"/continue", "continue"} or lowered.startswith("/continue "):
        return SessionCommand.continue_(_command_tail(text, "/continue"))
    if lowered in {"/cancel", "cancel", "/stop", "stop"} or lowered.startswith(("/cancel ", "/stop ")):
        tail = _command_tail(text, "/cancel") if lowered.startswith("/cancel") else _command_tail(text, "/stop")
        return SessionCommand.cancel(tail or "user_cancel")
    if lowered.startswith("/goal ") or lowered.startswith("/replace "):
        tail = _command_tail(text, "/goal") if lowered.startswith("/goal ") else _command_tail(text, "/replace")
        return SessionCommand.replace_goal(tail)
    return SessionCommand.message(text)


def parse_canonical_goal_command(line: str, *, has_active_goal: bool = False) -> SessionCommand | None:
    text = line.strip()
    if not text:
        return None
    if not _looks_like_strict_goal_command(text):
        return None
    target = parse_goal_target(text)
    if target is None:
        return None
    if not _canonical_goal_fully_matches(text, target):
        return None
    if has_active_goal:
        return SessionCommand.replace_goal(text, reason="chat_goal_promoted")
    return SessionCommand.start(text, reason="chat_goal_promoted")


def _looks_like_strict_goal_command(text: str) -> bool:
    lowered = text.strip().lower().replace("minecraft:", "")
    return bool(
        re.fullmatch(r"(?:collect|get|gather|mine)\s+(?:\d+\s+[a-z_]+|[a-z_]+\s+\d+)", lowered)
        or re.fullmatch(r"(?:craft|make|build)\s+(?:(?:\d+|a|an)\s+)?[a-z_]+(?:\s+[a-z_]+)*", lowered)
        or re.fullmatch(r"get\s+(?:an?\s+)?[a-z_]+", lowered)
    )


def _canonical_goal_fully_matches(text: str, target: GoalTarget) -> bool:
    lowered = text.strip().lower().replace("minecraft:", "")
    item_pattern = re.escape(target.item).replace("_", r"[_\s-]")
    count = str(target.count)
    if target.kind == "collect":
        return bool(
            re.fullmatch(rf"(?:collect|get|gather|mine)\s+{count}\s+{item_pattern}", lowered)
            or re.fullmatch(rf"(?:collect|get|gather|mine)\s+{item_pattern}\s+{count}", lowered)
        )
    return bool(
        re.fullmatch(rf"(?:craft|make|build)\s+(?:{count}\s+|a\s+|an\s+)?{item_pattern}", lowered)
        or (target.count == 1 and re.fullmatch(rf"get\s+(?:an?\s+)?{item_pattern}", lowered))
    )


def _command_tail(text: str, command: str) -> str:
    if text.lower().startswith(command):
        return text[len(command) :].strip()
    return ""


def _normalize_goal_text(goal: str) -> str:
    return " ".join(str(goal).strip().split()).casefold()


def is_ag_fp30_goal(goal: str) -> bool:
    """Return whether ``goal`` is the frozen primary AG-FP30 objective."""

    return _normalize_goal_text(goal) == _normalize_goal_text(AG_FP30_GOAL)


def _ag_fp30_target(goal: str) -> CompositeGoalTarget | None:
    if not is_ag_fp30_goal(goal):
        return None
    return CompositeGoalTarget(kind="production_terminal", goal_id=AG_FP30_GOAL_ID)


def _read_authoritative_inventory_slots(
    body: Body,
    *,
    page_size: int = 12,
) -> tuple[list[InventorySlot], str | None]:
    """Read every inventory page, preserving an explicit failure reason."""

    slots: list[InventorySlot] = []
    start: int | None = 0
    while start is not None:
        try:
            perception = body.perceive("inventory", {"start": start, "limit": page_size})
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"
        if not perception.ok:
            return [], perception.error or "inventory_perception_failed"
        try:
            slots.extend(
                InventorySlot.from_payload(payload)
                for payload in perception.data.get("slots") or []
                if isinstance(payload, dict)
            )
        except (TypeError, ValueError, KeyError) as exc:
            return [], f"invalid_inventory_slot: {exc}"
        next_value = perception.data.get("nextStart")
        if next_value is None:
            next_value = perception.next
        if next_value is None:
            if not perception.complete:
                return [], perception.error or "inventory_page_incomplete"
            start = None
        else:
            try:
                next_start = int(next_value)
            except (TypeError, ValueError):
                return [], "invalid_inventory_cursor"
            if next_start <= start:
                return [], "inventory_cursor_did_not_advance"
            start = next_start
    if not slots:
        # An empty inventory is a valid authoritative result, so this is not
        # an error.  The terminal predicates below will simply remain false.
        return [], None
    return slots, None


def _item_name(item: str | None) -> str | None:
    if item is None:
        return None
    return str(item).removeprefix("minecraft:").lower()


def evaluate_ag_fp30_terminal_truth(
    body: Body,
    goal: str,
    final: SessionStep,
) -> TerminalTruth:
    """Evaluate AG-FP30 from independent server-authoritative Body facts.

    The predicate intentionally reads inventory/equipment and the server-side
    owner head directly.  It never consumes model text, tool success flags, or
    the Agent's claimed plan.  A missing fact is an honest false result with a
    typed ``facts.error`` entry.
    """

    target = _ag_fp30_target(goal)
    if target is None:
        raise ValueError("evaluate_ag_fp30_terminal_truth requires the canonical AG-FP30 goal")

    facts: dict[str, object] = {
        "evaluator": AG_FP30_GOAL_ID,
        "goal_match": True,
        "inventory": {"ok": False},
        "flowers": {"distinct_items": [], "minimum": 3, "satisfied": False},
        "drops": {},
        "equipment": {
            "offhand": {"item": None, "required": "shield", "satisfied": False},
            "mainhand": {"item": None, "required": "iron_pickaxe", "selected_slot": None, "satisfied": False},
        },
        "body_owner": None,
        "pending_action_count": None,
        "terminal_satisfied": False,
    }
    slots, inventory_error = _read_authoritative_inventory_slots(body)
    if inventory_error is not None:
        facts["error"] = inventory_error
    else:
        counts: dict[str, int] = {}
        by_slot: dict[int, str] = {}
        slot_labels: dict[str, str] = {}
        for slot in slots:
            item = _item_name(slot.item)
            if item is None or slot.empty or slot.count <= 0:
                continue
            counts[item] = counts.get(item, 0) + slot.count
            by_slot[slot.slot] = item
            if slot.slot_label:
                slot_labels[slot.slot_label] = item

        flowers = sorted(item for item in AG_FP30_ACCEPTED_FLOWERS if counts.get(item, 0) > 0)
        facts["inventory"] = {
            "ok": True,
            "counts": {
                item: counts.get(item, 0)
                for item in sorted(
                    set(AG_FP30_ACCEPTED_FLOWERS)
                    | {"torch", "iron_ingot", "shield", "iron_pickaxe"}
                    | set().union(*AG_FP30_DROP_FAMILIES.values())
                )
                if counts.get(item, 0) > 0
            },
            "slot_count": len(slots),
        }
        facts["flowers"] = {
            "distinct_items": flowers,
            "minimum": 3,
            "satisfied": len(flowers) >= 3,
        }
        for entity, accepted in AG_FP30_DROP_FAMILIES.items():
            matched = sorted(item for item in accepted if counts.get(item, 0) > 0)
            facts["drops"][entity] = {
                "accepted_items": sorted(accepted),
                "matched_items": matched,
                "satisfied": bool(matched),
            }
        offhand = _item_name(slot_labels.get("offhand") or by_slot.get(40))
        facts["equipment"]["offhand"] = {
            "item": offhand,
            "required": "shield",
            "satisfied": offhand == "shield",
        }
        try:
            state = body.get_state()
            selected_slot = state.selected_slot
        except Exception as exc:
            selected_slot = None
            facts["equipment"]["mainhand"]["error"] = f"{type(exc).__name__}: {exc}"
        mainhand = None if selected_slot is None else by_slot.get(int(selected_slot))
        facts["equipment"]["mainhand"] = {
            "item": mainhand,
            "required": "iron_pickaxe",
            "selected_slot": selected_slot,
            "satisfied": mainhand == "iron_pickaxe",
        }
        facts["inventory"]["torch_count"] = counts.get("torch", 0)
        facts["inventory"]["iron_ingot_count"] = counts.get("iron_ingot", 0)

    try:
        head = body.event_head(f"terminal-{AG_FP30_GOAL_ID}")
        owner = head.get("owner")
        pending_count = head.get("pending_action_count")
        if pending_count is None:
            # The Body contract has one physical writer.  When the server
            # reports no owner there can be no pending physical action; retain
            # this fallback for older Scarpet versions that do not expose the
            # optional count field yet.
            pending_count = 0 if owner is None else 1
        facts["body_owner"] = owner
        facts["pending_action_count"] = int(pending_count)
    except Exception as exc:
        facts["owner_error"] = f"{type(exc).__name__}: {exc}"

    flowers_ok = bool(facts["flowers"].get("satisfied")) if isinstance(facts["flowers"], dict) else False
    drops_ok = (
        isinstance(facts["drops"], dict)
        and all(isinstance(value, dict) and bool(value.get("satisfied")) for value in facts["drops"].values())
    )
    equipment = facts["equipment"]
    equipment_ok = (
        isinstance(equipment, dict)
        and all(isinstance(value, dict) and bool(value.get("satisfied")) for value in equipment.values())
    )
    inventory = facts["inventory"]
    inventory_ok = (
        isinstance(inventory, dict)
        and bool(inventory.get("ok"))
        and int(inventory.get("torch_count", 0)) >= 16
        and int(inventory.get("iron_ingot_count", 0)) >= 3
    )
    lifecycle_ok = final.lifecycle not in {
        LifecycleState.YIELDED,
        LifecycleState.INTERRUPTED,
        LifecycleState.RECOVERING,
    }
    status_ok = final.status not in {"failed", "yielded"}
    owner_ok = facts["body_owner"] is None and facts["pending_action_count"] == 0
    satisfied = bool(flowers_ok and drops_ok and equipment_ok and inventory_ok and lifecycle_ok and status_ok and owner_ok)
    facts["terminal_satisfied"] = satisfied
    exit_code = _exit_code_for(final, satisfied=satisfied, has_target=True)
    return TerminalTruth(
        goal=goal,
        target=target,
        inventory_count=None,
        satisfied=satisfied,
        status=final.status,
        lifecycle=final.lifecycle.value,
        exit_code=exit_code,
        facts=facts,
    )


def evaluate_terminal_truth(body: Body, goal: str, final: SessionStep) -> TerminalTruth:
    ag_target = _ag_fp30_target(goal)
    if ag_target is not None:
        return evaluate_ag_fp30_terminal_truth(body, goal, final)
    target = parse_goal_target(goal)
    count: int | None = None
    satisfied = False
    if target is not None:
        count = sum(inventory_count(body, item) for item in target.inventory_items)
        satisfied = count >= target.count
    elif final.status == "completed_turn" and final.lifecycle is LifecycleState.ACTIVE:
        satisfied = False
    exit_code = _exit_code_for(final, satisfied=satisfied, has_target=target is not None)
    return TerminalTruth(
        goal=goal,
        target=target,
        inventory_count=count,
        satisfied=satisfied,
        status=final.status,
        lifecycle=final.lifecycle.value,
        exit_code=exit_code,
    )


def safe_evaluate_terminal_truth(
    body: Body,
    goal: str,
    final: SessionStep,
    *,
    session: AgentSession | None = None,
) -> TerminalTruth:
    try:
        return evaluate_terminal_truth(body, goal, final)
    except Exception as exc:
        parts = getattr(session, "parts", None) if session is not None else None
        if parts is not None:
            parts.runtime.trace.emit(
                "terminal_truth_failed",
                goal=goal,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        return TerminalTruth(
            goal=goal,
            target=_ag_fp30_target(goal) or parse_goal_target(goal),
            inventory_count=None,
            satisfied=False,
            status=final.status,
            lifecycle=final.lifecycle.value,
            exit_code=8,
        )


def _exit_code_for(final: SessionStep, *, satisfied: bool, has_target: bool) -> int:
    if final.status == "quit":
        return 0
    if satisfied:
        return 0
    if final.status == "failed":
        return 8
    if final.status == "yielded":
        return 5
    if final.lifecycle in {LifecycleState.YIELDED, LifecycleState.INTERRUPTED, LifecycleState.RECOVERING}:
        return 5
    if has_target:
        return 6
    return 7


def parse_collect_target(goal: str) -> CollectTarget | None:
    text = goal.strip().lower().replace("minecraft:", "")
    match = re.fullmatch(r"(?:collect|get|gather|mine)\s+(\d+)\s+([a-z_]+)", text)
    if match:
        return _collect_target(match.group(2), int(match.group(1)))
    match = re.fullmatch(r"(?:collect|get|gather|mine)\s+([a-z_]+)\s+(\d+)", text)
    if match:
        return _collect_target(match.group(1), int(match.group(2)))
    return None


def parse_goal_target(goal: str) -> GoalTarget | None:
    if not _looks_like_strict_goal_command(goal):
        return None
    collect = parse_collect_target(goal)
    if collect is not None:
        return GoalTarget(kind="collect", item=collect.item, count=collect.count, inventory_items=collect.inventory_items)

    parsed = _parse_acquire_goal(goal)
    if parsed is None:
        return None
    item, count = parsed
    return GoalTarget(kind="acquire", item=item, count=count, inventory_items=(item,))


def _collect_target(item: str, count: int) -> CollectTarget:
    plan = resource_plan_for(item)
    return CollectTarget(item=plan.requested_item, count=count, inventory_items=plan.inventory_items)


def _parse_acquire_goal(goal: str) -> tuple[str, int] | None:
    text = goal.strip().lower().replace("minecraft:", "")
    match = re.fullmatch(r"(?:craft|make|build)\s+(.+)", text)
    if match:
        return _parse_acquire_tail(match.group(1))
    match = re.fullmatch(r"get\s+(?:an?\s+)?([a-z_]+)", text)
    if match:
        return (_normalize_goal_item(match.group(1)), 1)
    return None


def _parse_acquire_tail(tail: str) -> tuple[str, int] | None:
    parts = tail.strip().split()
    if not parts:
        return None
    count = 1
    if parts[0].isdigit():
        count = int(parts.pop(0))
    elif parts[0] in {"a", "an"}:
        parts.pop(0)
    if not parts:
        return None
    return (_normalize_goal_item(" ".join(parts)), count)


def _normalize_goal_item(item: str) -> str:
    return re.sub(r"\s+", "_", item.strip().replace("-", "_"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MineBot Agent against an explicitly configured real Minecraft server.")
    parser.add_argument("goal", nargs="?", help="Natural-language user goal, e.g. 'collect 64 logs'.")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_RUNAWAY_STEP_LIMIT,
        help="Runaway guard for session steps; normal stopping is lifecycle/progress/terminal truth.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Keep the same real-server Agent session alive and read user messages from stdin.",
    )
    parser.add_argument(
        "--camera",
        action="store_true",
        help="Start the optional third-person Camera sidecar for this run.",
    )
    parser.add_argument(
        "--camera-config",
        type=Path,
        help="Optional Camera TOML override; defaults to the persistent local Camera config.",
    )
    args = parser.parse_args(argv)
    if not args.interactive and not args.goal:
        parser.error("goal is required unless --interactive is set")
    try:
        config = real_server_config_from_env()
    except (RealServerConfigError, AppConfigError, ValueError) as exc:
        print(f"Real-server agent config error: {exc}", file=sys.stderr)
        return 2
    if args.camera:
        from minebot.camera.config import resolve_camera_config_path

        camera_config = resolve_camera_config_path(args.camera_config)
    else:
        camera_config = None
    try:
        if args.interactive:
            return asyncio.run(
                run_real_server_interactive(
                    config,
                    args.goal,
                    max_steps=args.max_steps,
                    camera_config=camera_config,
                )
            )
        return asyncio.run(
            run_real_server_goal(
                config,
                args.goal,
                max_steps=args.max_steps,
                camera_config=camera_config,
            )
        )
    except AppConfigError as exc:
        print(f"Provider not configured: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AG_FP30_GOAL",
    "AG_FP30_GOAL_ID",
    "InteractiveScenarioContext",
    "InteractiveScenarioHook",
    "RealServerConfig",
    "RealServerConfigError",
    "env_required",
    "evaluate_ag_fp30_terminal_truth",
    "evaluate_terminal_truth",
    "is_ag_fp30_goal",
    "main",
    "parse_goal_target",
    "parse_collect_target",
    "parse_session_command",
    "real_server_config_from_env",
    "run_real_server_goal",
    "run_real_server_interactive",
]
