#!/usr/bin/env python3
"""Real-Body restart reconciliation gate without an external model request."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.body_events import BodyEventPump
from minebot.app.reconciliation import ReconcileDecision, enqueue_startup_reconciliation
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore, TaskStatus
from minebot.app.session import AgentSession
from minebot.app.tasks import TaskWorkspace
from minebot.app.work_queue import PersistentWorkIntentQueue, WorkIntentKind, WorkIntentState
from minebot.app.wiring import build_agent_runtime
from minebot.brain.registry import ToolRegistry
from minebot.contract import Action
from minebot.game import ScarpetBody
from tests.e2e_support import connect_or_skip, spawn_or_fail


BOT = "A4RestartBot"


def main() -> int:
    with connect_or_skip() as rcon, tempfile.TemporaryDirectory() as tmp:
        body = ScarpetBody(BOT, rcon)
        queue1 = None
        queue2 = None
        session = None
        try:
            spawn_or_fail(body, (0, 70, 0))
            scope = RuntimeScope("local-e2e", "restart-fixture", BOT)
            db_path = Path(tmp) / "agent-state.sqlite3"

            store1 = RuntimeStateStore(db_path)
            workspace1 = TaskWorkspace(store1, scope)
            task = workspace1.start("prepare for the End", source="restart_e2e")
            queue1 = PersistentWorkIntentQueue(store1, scope, lease_owner="process-1")
            queued = queue1.enqueue(
                WorkIntentKind.MESSAGE,
                source="user_message",
                payload={"text": "continue the task"},
                task_id=task.task_id,
            )
            leased = queue1.lease_next()
            assert leased is not None and leased.intent_id == queued.intent_id

            pos = body.get_state().pos
            move = Action.create(
                "moveTo",
                {
                    "target": [pos[0] + 40, pos[1], pos[2]],
                    "timeout_ticks": 400,
                },
            )
            accepted = body.execute(move)
            assert accepted.ok and accepted.accepted
            time.sleep(0.1)
            assert body.event_head("restart-e2e").get("owner") == "moveTo"

            queue1.close()
            queue1 = None
            store1.close()

            store2 = RuntimeStateStore(db_path)
            workspace2 = TaskWorkspace(store2, scope)
            queue2 = PersistentWorkIntentQueue(store2, scope, lease_owner="process-2")
            assert [item.intent_id for item in queue2.orphaned_intents] == [queued.intent_id]
            pump = BodyEventPump(body, queue2, store2, scope)
            reconciliation = enqueue_startup_reconciliation(
                body=body,
                event_pump=pump,
                queue=queue2,
                workspace=workspace2,
                orphaned_intents=queue2.orphaned_intents,
                app_reloaded=False,
            )
            assert reconciliation.decision is ReconcileDecision.RESUME
            assert body.event_head("restart-e2e").get("owner") is None

            model_calls = []

            def make_parts(goal: str):
                parts = build_agent_runtime(
                    body=body,
                    registry=ToolRegistry(),
                    goal_text=goal,
                    agent_name="RestartReconcileE2E",
                )

                async def fake_runner(_agent, input_text, **_kwargs):
                    model_calls.append(input_text)
                    return {"ok": True}

                parts.runtime.runner_run = fake_runner
                parts.runtime.runner_run_streamed = None
                return parts

            session = AgentSession(
                make_parts,
                task_workspace=workspace2,
                work_queue=queue2,
            )
            step = asyncio.run(session.step())
            assert step.status == "completed_turn"
            assert len(model_calls) == 1
            assert workspace2.current_task.status is TaskStatus.WAITING_EVENT
            stored = store2.get_work_intent(reconciliation.intent.intent_id)
            assert stored is not None and stored["state"] == WorkIntentState.COMPLETED.value
            assert queue2.pending_count() == 0
            print(
                {
                    "decision": reconciliation.decision.value,
                    "orphaned_intents": len(reconciliation.orphaned_intents),
                    "events": len(reconciliation.events),
                    "model_calls": len(model_calls),
                    "task_status": workspace2.current_task.status.value,
                    "owner_after": None,
                }
            )
            session.close()
            session = None
            queue2 = None
            store2.close()
            return 0
        finally:
            if session is not None:
                session.close()
            elif queue2 is not None:
                queue2.close()
            if queue1 is not None:
                queue1.close()
            try:
                body.interrupt("restart_e2e_cleanup")
                body.despawn()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
