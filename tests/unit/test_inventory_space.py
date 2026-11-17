"""How full the inventory is, said in idle, running and result frames (vision-and-home-20261028.md §7.7, Decision)."""
import asyncio

from minebot.app import reflex_question as rq
from tests.unit.test_home_dimension import loop_with


def slots(used_main, armour=0):
    rows = []
    for slot in range(46):
        kind = "hotbar" if slot < 9 else "inventory" if slot < 36 else "armor" if slot < 40 else (
            "offhand" if slot == 40 else "aux")
        full = slot < used_main if slot < 36 else 36 <= slot < 36 + armour
        rows.append({"slot": slot, "slotType": kind, "empty": not full, "count": 1 if full else 0,
                     **({"item": "minecraft:cobblestone"} if full else {})})
    return rows


def test_only_the_36_hotbar_and_inventory_slots_are_counted():
    assert rq.inventory_slots_used({"inventory_slots": slots(30, armour=4)}) == [30, 36]
    assert rq.inventory_slots_used({"inventory_slots": []}) is None
    assert rq.inventory_slots_used({}) is None
    assert rq.inventory_space({"inventory_slots_used": [30, 36]}) == "30 of 36 inventory slots are used."
    assert rq.inventory_space({"inventory_slots": slots(30)}) is None


def test_idle_running_and_result_frames_say_it_after_the_inventory():
    facts = {"health": 20, "food": 20, "inventory": {"cobblestone": 64}, "inventory_slots_used": [36, 36],
             "position": [0, 64, 0]}
    idle = rq.idle_question(facts, {"stay": "Stay where the Body is and wait for the next step."})["state"]
    keys = list(idle)
    assert idle["inventory_space"] == "36 of 36 inventory slots are used."
    assert keys.index("inventory_space") == keys.index("inventory") + 1
    for running in (True, False):
        state = rq.frame_state(goal="", intent="", tool="move_to", params={}, facts=facts, running=running,
                               result={"success": True, "reason": "completed"})
        keys = list(state)
        assert state["inventory_space"] == "36 of 36 inventory slots are used."
        assert keys.index("inventory_space") == keys.index("inventory") + 1
    without = {key: value for key, value in facts.items() if key != "inventory_slots_used"}
    assert "inventory_space" not in rq.idle_question(without, {"stay": "Stay."})["state"]
    assert "inventory_space" not in rq.frame_state(goal="", intent="", tool="move_to", params={}, facts=without,
                                                   running=True)


def test_a_body_read_carries_the_count_and_running_frames_keep_it_with_the_inventory(tmp_path):
    loop, _, _ = loop_with(tmp_path)

    class Lane:
        async def run(self, function, *args):
            return function(*args)

    raw = {"health": 20, "dimension": "minecraft:overworld", "position": [0, 64, 0],
           "inventory": {"cobblestone": 12}, "inventory_slots": slots(12)}
    loop.runtime.body = type("Body", (), {"get_reflex_facts": staticmethod(lambda: dict(raw))})()
    loop.runtime._read_lane = Lane()
    read = asyncio.run(loop._read_body_facts())
    assert read["inventory_slots_used"] == [12, 36]
    assert "inventory_slots_used" in loop._AMBIENT_KEYS
    assert loop._frame({"facts": read})["facts"]["inventory_slots_used"] == [12, 36]
    # A later frame without a fresh read keeps it ...
    assert loop._frame({"facts": {"health": 20}})["facts"]["inventory_slots_used"] == [12, 36]
    # ... but an inventory reported without it drops it rather than pair it with the old count.
    assert "inventory_slots_used" not in loop._frame({"facts": {"inventory": {"cobblestone": 40}}})["facts"]
