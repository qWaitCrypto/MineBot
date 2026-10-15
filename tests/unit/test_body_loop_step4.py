"""Death transport, deterministic settlement, and the existing SDK recovery path.

Mocks here test plumbing only; real-client acceptance is recorded separately.
"""
import asyncio
import copy
import json
import socket
import struct
import threading
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop
from minebot.app.runner import AgentRuntime, RecoveryOutcome
from minebot.app.session import AgentSession
from minebot.brain.lifecycle import LifecycleState
from minebot.contract import Event
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc
from tests.unit.test_agent_session import build_parts
from tests.unit.test_real_client_body import FakeClientIpc


def death(seq=1):
    return Event(seq=seq, tick=20, bot="Bot", name="death", data={
        "position": [4, 60, 8], "health": 0, "oxygen": -1, "dimension": "overworld",
        "head_in_liquid": True, "native_epoch": "client", "native_seq": seq,
        "last_living_state": {"health": 2, "oxygen": 0, "position": [4, 60, 8]},
    })


def test_disconnected_native_sample_does_not_cancel_work_or_notify_death():
    async def run():
        class DisconnectedIpc(FakeClientIpc):
            def state(self):
                return {"ready": False}
        events, stops = [], []
        owner = SimpleNamespace(decision_client=SimpleNamespace(),
            trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
            notify_body_lifecycle_event=events.append)
        async def stop(reason):
            stops.append(reason)
            return {"settled": True}
        owner.cancel_active_execution_with_facts = stop
        loop = BodyLoop(owner)
        loop._set_state("work", "agent_call")
        loop.current = {"cancel_requested": False}
        facts = RealClientBody("Bot", DisconnectedIpc()).get_reflex_facts()
        assert not await loop._process_lifecycle_events(facts)
        assert loop.loop_state == "work"
        assert not stops and not events
        assert loop.current["cancel_requested"] is False
        # Actual native death remains authoritative after a read gap.
        loop._queue_lifecycle_event(death())
        assert await loop._process_lifecycle_events()
        assert stops == ["death"] and [e.name for e in events] == ["death"]
    asyncio.run(run())


def test_native_lifecycle_frames_do_not_steal_correlated_action_replies():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    def send(connection, frame):
        payload = json.dumps(frame).encode()
        connection.sendall(struct.pack(">I", len(payload)) + payload)
    def run_server():
        connection, _ = server.accept()
        with connection:
            size = struct.unpack(">I", connection.recv(4))[0]
            request = json.loads(connection.recv(size))
            send(connection, {"type": "accepted", "action_id": request["action_id"]})
            frame = {"type": "body_event", "epoch": "client", "seq": 1,
                     "tick": 20, "name": "death", "data": death().data}
            send(connection, frame)
            send(connection, frame)  # buffered reconnect replay is deduplicated
            send(connection, {"type": "terminal", "action_id": request["action_id"],
                              "status": "failed", "reason": "death"})
            send(connection, {"type": "body_event", "epoch": "client", "seq": 2,
                              "tick": 21, "name": "respawned", "data": {"health": 20, "position": [0, 64, 0]}})
        server.close()
    worker = threading.Thread(target=run_server)
    worker.start()
    with RealClientIpc(port=port) as ipc:
        body = RealClientBody(bot_name="Bot", ipc=ipc)
        terminal = ipc.command("goto:1:64:1", action_id="work")
        worker.join()
        # The reader may still be dispatching the last queued frame.
        seen = threading.Event()
        body.add_lifecycle_listener(lambda event: seen.set() if event.name == "respawned" else None)
        assert seen.wait(1)
        assert terminal.reason == "death"
        events = body.poll_events()
        assert [e.name for e in events] == ["death", "respawned"]
        assert events[0].data["position"] == [4, 60, 8]
        assert events[0].seq < events[1].seq


def test_death_clears_work_and_suspended_protection_before_sdk_notification():
    async def run():
        rows, notifications, stops = [], [], []
        owner = SimpleNamespace(decision_client=SimpleNamespace(),
            trace=SimpleNamespace(emit=lambda event, **data: rows.append((event, data))))
        loop = BodyLoop(owner)
        loop._set_state("survival", "decision")
        loop.safe_standing_position = [0, 64, 0]
        loop._result_pending["work"] = {}
        loop._suspended = {"call_id": "work"}
        loop.current = {"cancel_requested": False}
        protection = asyncio.create_task(asyncio.sleep(10))
        loop._protection_task = protection
        async def stop(reason):
            assert loop.loop_state == "dead" and not loop._result_pending
            assert loop._suspended is None and loop.safe_standing_position is None
            stops.append(reason)
            return {"settled": True}
        owner.cancel_active_execution_with_facts = stop
        owner.notify_body_lifecycle_event = notifications.append
        loop._queue_lifecycle_event(death())
        assert await loop._process_lifecycle_events()
        assert protection.cancelled()
        assert stops == ["death"] and notifications[0].name == "death"
        assert loop.current["result_abandoned"] is True
        loop._set_state("work", "call_ended")
        assert loop.loop_state == "dead"
        loop._queue_lifecycle_event(death())
        await loop._process_lifecycle_events()
        assert stops == ["death"]
        loop._queue_lifecycle_event(Event(seq=2, tick=21, bot="Bot", name="respawned", data={"health": 20}))
        assert not await loop._process_lifecycle_events()
        assert loop.loop_state == "idle"
        assert [e.name for e in notifications] == ["death", "respawned"]
    asyncio.run(run())


def test_native_death_and_respawn_are_processed_even_with_a_pending_result_slot():
    async def run():
        delivered = asyncio.Event()
        stop_count, events = [], []
        listeners = []
        owner = SimpleNamespace(decision_client=SimpleNamespace(),
            trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
            body=SimpleNamespace(add_lifecycle_listener=listeners.append,
                                 remove_lifecycle_listener=listeners.remove))
        async def stop(reason):
            stop_count.append(reason)
            return {"settled": True}
        owner.cancel_active_execution_with_facts = stop
        def notify(event):
            events.append(event)
            if event.name == "respawned": delivered.set()
        owner.notify_body_lifecycle_event = notify
        loop = BodyLoop(owner)
        await loop.start()
        loop._result_pending["old-result"] = {}
        listeners[0](death())
        listeners[0](Event(seq=2, tick=21, bot="Bot", name="respawned", data={"health": 20}))
        await asyncio.wait_for(delivered.wait(), 1)
        assert stop_count == ["death"] and loop.loop_state == "idle"
        assert not loop._result_pending
        await loop.stop()
        assert listeners == []
    asyncio.run(run())


def test_a_dead_frame_has_no_model_request_and_late_answers_are_discarded():
    async def run():
        requests = []
        owner = SimpleNamespace(decision_client=SimpleNamespace(),
            trace=SimpleNamespace(emit=lambda *args, **kwargs: None))
        async def stop(reason): return {"settled": True}
        owner.cancel_active_execution_with_facts = stop
        loop = BodyLoop(owner)
        async def judge(*args, **kwargs):
            requests.append(True)
            loop._queue_lifecycle_event(death())
            return "surface"
        loop.judge = judge
        await loop._process_lifecycle_events({"health": 0})
        assert await loop._judge_frame("x", None, {}, {}, 0, purpose="running_frame", supervision_context={}) == "unknown"
        assert not requests
        loop._queue_lifecycle_event(Event(seq=2, tick=21, bot="Bot", name="respawned", data={"health": 20}))
        await loop._process_lifecycle_events()
        assert await loop._judge_frame("x", None, {}, {}, 0, purpose="running_frame", supervision_context={}) == "unknown"
        assert len(requests) == 1 and loop.loop_state == "dead"
    asyncio.run(run())


def test_sdk_lifecycle_delivery_reuses_body_event_and_recovery_queues():
    async def run():
        calls, bodies = [], []
        session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
        await session.enter_world()
        runtime = session.parts.runtime
        runtime.recovery_handler = lambda runtime: RecoveryOutcome(True, "body_reconciled", facts={"health": 20})
        edge = death()
        edge.data["inventory_slots"] = [{"noise": "x" * 40000}]
        edge.data["entities"] = [{"hostile": False, "noise": "x" * 40000},
                                  {"hostile": True, "id": "near", "dist2": 4, "type": "zombie"},
                                  {"hostile": True, "id": "far", "dist2": 400, "type": "zombie"}]
        runtime.notify_body_lifecycle_event(edge)
        first = await session.step()
        assert first.lifecycle is LifecycleState.RECOVERING
        assert calls == []  # recovery admission, not an extra planning authority
        second = await session.step()
        assert second.lifecycle is LifecycleState.RESUMING
        third = await session.step()
        assert third.lifecycle in {LifecycleState.ACTIVE, LifecycleState.IDLE}
        assert len(calls) == 1
        assert "BODY_EVENT:" in calls[0] and '"name": "death"' in calls[0]
        assert '"position": [4, 60, 8]' in calls[0]
        assert '"id": "near"' in calls[0]
        assert '"count": 2' in calls[0]
        assert '"health": 2' in calls[0]
        assert "inventory_slots" not in calls[0] and "noise" not in calls[0]
        assert len(calls[0]) < 32000  # actual context reaches the model unbounded
        assert len(edge.data["inventory_slots"][0]["noise"]) == 40000
        assert runtime._pending_body_death is None
        await runtime.aclose()
    asyncio.run(run())


def test_active_stream_watch_observes_death_even_after_health_has_recovered():
    async def run():
        owner = SimpleNamespace(_pending_body_death=copy.deepcopy(death().data))
        reason, facts = await AgentRuntime._wait_for_critical_body_fact(owner)
        assert reason == "death" and facts["position"] == [4, 60, 8]
        assert owner._pending_body_death is None
    asyncio.run(run())


def test_death_reaches_the_agent_before_the_old_action_settles():
    # Live step7-f: settling the dead Body's action took 35.5 s, and the
    # Agent heard of the death only after it. The death facts never needed it.
    async def run():
        rows, notifications = [], []
        release = asyncio.Event()
        owner = SimpleNamespace(decision_client=SimpleNamespace(),
            trace=SimpleNamespace(emit=lambda event, **data: rows.append(event)))
        async def stop(reason):
            await release.wait()
            return {"settled": True}
        owner.cancel_active_execution_with_facts = stop
        owner.notify_body_lifecycle_event = notifications.append
        loop = BodyLoop(owner)
        loop.current = {"cancel_requested": False}
        loop._queue_lifecycle_event(death())
        processing = asyncio.create_task(loop._process_lifecycle_events())
        for _ in range(20):
            await asyncio.sleep(0)
        assert [e.name for e in notifications] == ["death"]
        assert "reflex_death" in rows and "reflex_death_settled" not in rows
        release.set()
        assert await processing
        assert rows.index("reflex_death") < rows.index("reflex_death_settled")
    asyncio.run(run())
