"""Agent session entrypoint for an existing real Minecraft server.

Unlike the local console, this module must not prepare, reset, teleport, clear,
seed resources, or change gamerules. It only connects to an explicitly
configured real-server RCON endpoint and drives the Agent session.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from minebot.app.config import AppConfigError, agent_language_from_env, provider_registry_from_env
from minebot.app.observability import JsonlObservationSink
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime, inventory_count
from minebot.app.runner import RuntimeTrace
from minebot.app.wiring import AgentRuntimeParts
from minebot.app.session import DEFAULT_RUNAWAY_STEP_LIMIT, AgentSession, SessionCommand, SessionCommandKind, SessionStep
from minebot.brain.lifecycle import LifecycleState
from minebot.brain.modes import AgentSignal
from minebot.brain.composition import resource_plan_for
from minebot.contract import Body, Region
from minebot.game import RconClient, ScarpetBody
from minebot.game.errors import EnvelopeError, RconError
from minebot.game.protocol import build_state_call, parse_state
from minebot.game.rcon import RconConfig


@dataclass(frozen=True)
class RealServerConfig:
    rcon: RconConfig
    bot_name: str
    natural_region: Region
    recovery_respawn_pos: tuple[int, int, int] | None
    log_path: Path
    language: str


class RealServerConfigError(RuntimeError):
    pass


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
class TerminalTruth:
    goal: str
    target: GoalTarget | CollectTarget | None
    inventory_count: int | None
    satisfied: bool
    status: str
    lifecycle: str
    exit_code: int

    def to_trace(self) -> dict[str, object]:
        target_payload: dict[str, object] | None = None
        if self.target is not None:
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
    return RealServerConfig(
        rcon=RconConfig(host=host, port=port, password=password, timeout_s=timeout_s),
        bot_name=bot_name,
        natural_region=natural_region,
        recovery_respawn_pos=recovery_respawn_pos,
        log_path=log_path,
        language=language,
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


async def run_real_server_goal(config: RealServerConfig, goal: str, *, max_steps: int | None) -> int:
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

        session = AgentSession(make_parts, goal_driver=_goal_driver)
        session.submit(SessionCommand.start(goal))
        try:
            final = await session.run_until_waiting(
                max_steps=max_steps,
                should_stop=lambda step: safe_evaluate_terminal_truth(body, goal, step, session=session).satisfied,
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
            await provider.aclose()


async def run_real_server_interactive(config: RealServerConfig, goal: str | None, *, max_steps: int | None) -> int:
    """Run one persistent real-server session with stdin as the user channel."""
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
                ),
                agent_name="MineBotRealServer",
                language=config.language,
                trace=trace,
            )
            return parts

        session = AgentSession(make_parts, goal_driver=_goal_driver)
        if goal:
            session.submit(SessionCommand.start(goal))
        reader = asyncio.create_task(_stdin_command_reader(session))
        try:
            final = await _run_interactive_loop(
                session,
                fallback_goal=goal,
                body=body,
                chat_source=body,
                max_steps=max_steps,
            )
            terminal_goal = _session_goal(session, goal)
            truth = safe_evaluate_terminal_truth(body, terminal_goal, final, session=session)
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
            reader.cancel()
            await provider.aclose()


def _ensure_scarpet_global_app(rcon: RconClient, bot_name: str) -> None:
    rcon.request("script load minebot global")
    command = build_state_call(bot_name)
    parse_state(rcon.request(command))


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
        if truth.target is not None and truth.inventory_count is not None:
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
) -> SessionStep:
    last = None
    remaining = max_steps
    while remaining is None or remaining > 0:
        _poll_chat_commands(session, chat_source)
        last = await session.step()
        if last.status == "quit":
            return last
        truth = safe_evaluate_terminal_truth(body, _session_goal(session, fallback_goal), last, session=session)
        if truth.satisfied:
            completed = session.complete_current_goal("terminal_truth_satisfied")
            completed_truth = safe_evaluate_terminal_truth(body, truth.goal, completed, session=session)
            _announce_interactive_terminal(body, completed_truth)
            last = completed
        if last.lifecycle not in {LifecycleState.ACTIVE, LifecycleState.RECOVERING, LifecycleState.RESUMING} and not session.pending:
            await asyncio.sleep(0.25)
            if remaining is not None:
                remaining -= 1
            continue
        if remaining is not None:
            remaining -= 1
        await asyncio.sleep(0.25 if last.lifecycle.value == "idle" else 0)
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
        parts = getattr(session, "parts", None)
        if parts is not None:
            parts.runtime.trace.emit("chat_poll_failed", error_type=type(exc).__name__)
        return 0
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
            promoted = parse_canonical_goal_command(message, has_active_goal=getattr(session, "parts", None) is not None)
            if promoted is not None:
                command = promoted
            elif getattr(session, "parts", None) is None or _session_accepts_idle_start(session):
                command = SessionCommand.start(message, reason="chat_session_started")
        if command is None:
            continue
        parts = getattr(session, "parts", None)
        if parts is not None:
            parts.runtime.trace.emit(
                "chat_message",
                sender=str(data.get("sender") or ""),
                command=command.kind.value,
                content=command.text,
                reason=command.reason,
            )
        session.submit(command)
        count += 1
    return count


def _session_goal(session: AgentSession, fallback: str | None) -> str:
    return getattr(session, "current_goal", None) or fallback or ""


def _session_accepts_idle_start(session: AgentSession) -> bool:
    lifecycle = getattr(session, "lifecycle_state", None)
    if lifecycle is None:
        return False
    return lifecycle is LifecycleState.IDLE and not getattr(session, "pending", None)


def _goal_driver(parts: AgentRuntimeParts, signals: list[AgentSignal]) -> SessionStep | None:
    target = parse_goal_target(parts.context.goal_text)
    if target is None:
        parts.runtime.trace.emit("goal_driver_skipped", reason="no_supported_goal_target", goal=parts.context.goal_text)
        return None
    if target.kind == "collect":
        outcome = parts.runtime.drive_tool_once(
            "collect_resource",
            {"item": target.item, "count": target.count},
            reason="canonical_collect_goal",
            extra_signals=signals,
        )
        return SessionStep(outcome.status, outcome.lifecycle, outcome.message)
    outcome = parts.runtime.drive_tool_once(
        "ensure_tool_for",
        {"resource": target.item},
        reason="canonical_acquire_goal",
        extra_signals=signals,
    )
    return SessionStep(outcome.status, outcome.lifecycle, outcome.message)


async def _stdin_command_reader(session: AgentSession) -> None:
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            return
        command = parse_session_command(line)
        if command is not None:
            session.submit(command)


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


def evaluate_terminal_truth(body: Body, goal: str, final: SessionStep) -> TerminalTruth:
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
            target=parse_goal_target(goal),
            inventory_count=None,
            satisfied=False,
            status=final.status,
            lifecycle=final.lifecycle.value,
            exit_code=8,
        )


def _exit_code_for(final: SessionStep, *, satisfied: bool, has_target: bool) -> int:
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
    match = re.search(r"\b(?:collect|get|gather|mine)\s+(\d+)\s+([a-z_]+)\b", text)
    if match:
        return _collect_target(match.group(2), int(match.group(1)))
    match = re.search(r"\b(?:collect|get|gather|mine)\s+([a-z_]+)\s+(\d+)\b", text)
    if match:
        return _collect_target(match.group(1), int(match.group(2)))
    return None


def parse_goal_target(goal: str) -> GoalTarget | None:
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
    match = re.search(r"\b(?:craft|make|build)\s+(.+)$", text)
    if match:
        return _parse_acquire_tail(match.group(1))
    match = re.search(r"\bget\s+(?:an?\s+)?([a-z_]+)\b", text)
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
    args = parser.parse_args(argv)
    if not args.interactive and not args.goal:
        parser.error("goal is required unless --interactive is set")
    try:
        config = real_server_config_from_env()
    except (RealServerConfigError, AppConfigError, ValueError) as exc:
        print(f"Real-server agent config error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.interactive:
            return asyncio.run(run_real_server_interactive(config, args.goal, max_steps=args.max_steps))
        return asyncio.run(run_real_server_goal(config, args.goal, max_steps=args.max_steps))
    except AppConfigError as exc:
        print(f"Provider not configured: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RealServerConfig",
    "RealServerConfigError",
    "env_required",
    "evaluate_terminal_truth",
    "main",
    "parse_goal_target",
    "parse_collect_target",
    "parse_session_command",
    "real_server_config_from_env",
    "run_real_server_goal",
    "run_real_server_interactive",
]
