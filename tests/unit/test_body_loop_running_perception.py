"""An attacker appearing during a lite movement must enter the next question."""
import asyncio
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop
from minebot.app.execution_snapshot import ExecutionSnapshots
from minebot.app import reflex_question as rq


def runtime_for(getter, perceive):
    async def read(callback, *args):
        return callback(*args)

    snapshots = ExecutionSnapshots()
    snapshots.begin("walk", "move_to", {"pos": [30, 64, 0]})
    snapshots.observe("walk", {"position": [0, 64, 0], "entities": [],
                               "inventory": {}, "health": 20, "oxygen": 300})
    return SimpleNamespace(decision_client=SimpleNamespace(),
        trace=SimpleNamespace(emit=lambda *a, **k: None), execution_snapshots=snapshots,
        body=SimpleNamespace(get_reflex_facts=getter, perceive=perceive),
        _read_lane=SimpleNamespace(run=read), direct_call_handoffs={})


def test_running_question_reads_new_attacker_instead_of_carrying_empty_entities():
    async def run():
        hostile = {"id": "new-attacker", "hostile": True, "name": "Hostile",
                   "pos": [4, 64, 0], "dist2": 9}
        owner = runtime_for(
            lambda: {"position": [1, 64, 0], "health": 6, "food": 10, "oxygen": 300,
                     "inventory": {}, "on_ground": True, "head_in_liquid": False},
            lambda *args: {"ok": True, "data": {"entities": [hostile]}},
        )
        loop = BodyLoop(owner)
        loop._life_profile = None
        record = {"call_id": "walk", "tool": "move_to", "params": {"pos": [30, 64, 0]},
                  "cancel_requested": False, "questions": 0, "reads": 0,
                  "unknown_answers": 0, "initial_reader_done": True,
                  "supervision_context": {"intent": "Reach [30,64,0]."}}
        loop.current = record
        finish = asyncio.Event()
        worker = asyncio.create_task(finish.wait())
        wake = asyncio.Event()
        wake.set()
        seen = []

        async def judge(call, tool, params, frame, idle_s, **kwargs):
            seen.append(frame)
            assert rq.hostiles(frame["facts"])[0]["id"] == "new-attacker"
            assert frame["facts"]["health"] == 6
            assert "move_away" in kwargs["options"]
            finish.set()
            return "keep_going"

        loop.judge = judge
        try:
            await asyncio.wait_for(loop._monitor(record, SimpleNamespace(name="move_to"),
                                                record["params"], wake, worker), .5)
            assert seen
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_fresh_perception_clears_disappeared_target_and_read_after_terminal_is_ignored():
    async def run():
        entities = [{"id": "old", "hostile": True, "dist2": 4}]
        owner = runtime_for(lambda: {"health": 20, "inventory": {}},
                            lambda *args: {"ok": True, "data": {"entities": list(entities)}})
        loop = BodyLoop(owner)
        await loop._refresh_running_facts("walk")
        assert rq.hostiles(owner.execution_snapshots.current()["facts"])
        entities.clear()
        await loop._refresh_running_facts("walk")
        assert not rq.hostiles(owner.execution_snapshots.current()["facts"])
        owner.execution_snapshots.finish("walk", state="succeeded", reason="arrived", control="ended")
        terminal = owner.execution_snapshots.last()
        entities.append({"id": "late", "hostile": True, "dist2": 1})
        await loop._refresh_running_facts("walk")
        assert owner.execution_snapshots.current() is None
        assert owner.execution_snapshots.last() == terminal

    asyncio.run(run())


def test_body_read_refreshes_inventory_without_overwriting_action_motion_or_phase():
    async def run():
        owner = runtime_for(lambda: {"position": [0, 64, 0], "pathing": False,
            "inventory": {"apple": 1}, "inventory_complete": True, "selected_item": "apple"},
            lambda *args: {"ok": True, "data": {"entities": []}})
        owner.execution_snapshots.observe("walk", {"position": [8, 64, 0], "pathing": True,
            "phase": "moving", "navigation": {"src": [8, 64, 0], "dest": [9, 64, 0]}})
        loop = BodyLoop(owner)
        await loop._refresh_running_facts("walk")
        snapshot = owner.execution_snapshots.current()
        assert snapshot["phase"] == "moving"
        assert snapshot["facts"]["position"] == [8, 64, 0]
        assert snapshot["facts"]["pathing"] is True
        assert rq._inventory(snapshot["facts"]) == {"apple": 1}
        assert snapshot["facts"]["selected_item"] == "apple"
    asyncio.run(run())
