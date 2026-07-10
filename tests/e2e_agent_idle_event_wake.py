#!/usr/bin/env python3
"""Real-server idle/event wake gate with a deterministic fake model runner."""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_events import BodyEventPump
from minebot.app.reconciliation import ReconcileDecision, enqueue_startup_reconciliation
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.session import AgentSession
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import PersistentWorkIntentQueue
from minebot.app.wiring import build_agent_runtime
from minebot.brain.registry import ToolRegistry
from minebot.game import ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "A4IdleWakeBot"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idle-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)

    with connect_or_skip() as rcon, tempfile.TemporaryDirectory() as tmp:
        body = ScarpetBody(BOT, rcon)
        session = None
        queue = None
        try:
            spawn_or_fail(body, (0, 70, 0))
            rcon.request("difficulty peaceful")
            rcon.request(f"gamemode creative {BOT}")
            scope = RuntimeScope("local-e2e", "idle-wake-fixture", BOT)
            store = RuntimeStateStore(Path(tmp) / "agent-state.sqlite3")
            workspace = TaskWorkspace(store, scope)
            queue = PersistentWorkIntentQueue(store, scope)
            pump = BodyEventPump(body, queue, store, scope)
            startup = enqueue_startup_reconciliation(
                body=body,
                event_pump=pump,
                queue=queue,
                workspace=workspace,
                orphaned_intents=queue.orphaned_intents,
                app_reloaded=False,
            )
            assert startup.decision is ReconcileDecision.IDLE

            model_calls = []

            def make_parts(goal: str):
                parts = build_agent_runtime(
                    body=body,
                    registry=ToolRegistry(),
                    goal_text=goal,
                    agent_name="IdleWakeE2E",
                )

                async def fake_runner(_agent, input_text, **_kwargs):
                    model_calls.append(input_text)
                    return {"ok": True}

                parts.runtime.runner_run = fake_runner
                parts.runtime.runner_run_streamed = None
                return parts

            session = AgentSession(
                make_parts,
                task_workspace=workspace,
                work_queue=queue,
            )
            queue = None
            startup_step = asyncio.run(session.step())
            assert startup_step.status == "idle"
            assert model_calls == []

            deadline = time.monotonic() + max(0.0, args.idle_seconds)
            idle_polls = 0
            ambient_events = 0
            while time.monotonic() < deadline:
                result = pump.poll_once(
                    task_id=None,
                    generation=session.parts.authority.current_generation(),
                )
                idle_polls += 1
                ambient_events += result.observed
                assert result.enqueued == 0
                assert not session.has_pending_work
                assert model_calls == []
                time.sleep(0.25)

            rcon.request(
                "script in minebot run emit('underAttack', "
                f"'{BOT}', l('test_attacker', 20, 20))"
            )
            wake_poll = pump.poll_once(
                task_id=None,
                generation=session.parts.authority.current_generation(),
            )
            assert wake_poll.enqueued == 1
            wake_step = asyncio.run(session.step())
            assert wake_step.status == "completed_turn"
            assert len(model_calls) == 1

            replay_poll = pump.poll_once(
                task_id=None,
                generation=session.parts.authority.current_generation(),
            )
            assert replay_poll.enqueued == 0
            assert not session.has_pending_work
            assert len(model_calls) == 1
            print(
                {
                    "idle_seconds": args.idle_seconds,
                    "idle_polls": idle_polls,
                    "ambient_events": ambient_events,
                    "idle_model_calls": 0,
                    "wake_event": "underAttack",
                    "wake_model_calls": len(model_calls),
                    "replay_enqueued": replay_poll.enqueued,
                }
            )
            session.close()
            session = None
            store.close()
            return 0
        finally:
            if session is not None:
                session.close()
            elif queue is not None:
                queue.close()
            try:
                body.interrupt("idle_wake_e2e_cleanup")
                body.despawn()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
