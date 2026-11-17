"""Going home across dimensions and the portal pairs it needs (vision-and-home-20261028.md §7.7 item 4)."""
import asyncio
from types import SimpleNamespace

from minebot.app import reflex_question as rq
from minebot.app.body_loop import BodyLoop
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app.life_profile import LifeStore
from minebot.app.runtime_state import RuntimeScope
from minebot.contract import Event

HOME = {"pos": [100, 64, 100]}
PORTAL = {"from": {"dimension": "minecraft:overworld", "pos": [120, 64, 100]},
          "to": {"dimension": "minecraft:the_nether", "pos": [15, 70, 12]}, "t": 1.0}


def facts(dimension, pos, **extra):
    return {"dimension": dimension, "position": pos, "health": 20, "life_profile": {"home": HOME}, **extra}


def test_in_the_homes_dimension_going_home_is_worded_as_before():
    options = rq.idle_options(facts("minecraft:overworld", [100, 64, 130]))
    assert options["go_home"] == "Walk to the home position [100.0, 64.0, 100.0]."
    # A frame that does not say its dimension is taken as the home's.
    assert "go_home" in rq.idle_options({"position": [100, 64, 130], "life_profile": {"home": HOME}})
    assert rq.action_intent("go_home", facts("overworld", [0, 64, 0])) == "Walk to the home position."


def test_from_another_dimension_going_home_walks_into_a_known_portal():
    nether = facts("minecraft:the_nether", [20, 70, 12], known_portals=[PORTAL])
    options = rq.idle_options(nether)
    assert options["go_home"] == ("Walk into the portal at [15.0, 70.0, 12.0]; it leads to the overworld, "
                                  "where home is.")
    assert CallSupervisor._default_action("go_home", {"facts": nether}) == (
        "move_to", {"pos": [15, 70, 12], "radius": 0, "timeout_s": 60.0})
    assert rq.action_intent("go_home", nether) == rq.DEFAULT_ACTION_INTENTS["go_home_portal"]
    # The nearest of several ends on this side.
    other = {"from": {"dimension": "minecraft:the_nether", "pos": [200, 70, 200]},
             "to": {"dimension": "minecraft:overworld", "pos": [1600, 64, 1600]}}
    assert rq.home_portal({**nether, "known_portals": [other, PORTAL]}) == [15, 70, 12]


def test_without_a_known_portal_going_home_is_not_offered():
    nether = facts("minecraft:the_nether", [20, 70, 12])
    assert "go_home" not in rq.idle_options(nether)
    assert CallSupervisor._default_action("go_home", {"facts": nether}) is None
    # A portal between two other dimensions is no way home.
    end = {"from": {"dimension": "minecraft:the_end", "pos": [0, 64, 0]},
           "to": {"dimension": "minecraft:the_nether", "pos": [1, 70, 1]}}
    assert "go_home" not in rq.idle_options({**nether, "known_portals": [end]})


def test_the_same_numbers_in_another_dimension_are_not_home():
    assert rq.at_home(facts("minecraft:overworld", [100, 64, 100]))
    assert not rq.at_home(facts("minecraft:the_nether", [100, 64, 100]))
    nether_home = {**facts("minecraft:the_nether", [100, 64, 100]),
                   "life_profile": {"home": {**HOME, "dimension": "minecraft:the_nether"}}}
    assert rq.at_home(nether_home) and rq.home_here(nether_home)


def loop_with(tmp_path):
    events = []
    runtime = SimpleNamespace(trace=SimpleNamespace(emit=lambda event, **kw: events.append((event, kw))),
                              decision_client=SimpleNamespace(), last_known_body_state={},
                              execution_snapshots=SimpleNamespace(current=lambda: None))
    loop = BodyLoop(runtime)
    store = LifeStore(tmp_path, RuntimeScope("s", "w", "Bot"))
    loop.attach_life_store(store)
    return loop, store, events


def alive(dimension, pos):
    return {"dimension": dimension, "position": pos, "health": 20}


def test_a_dimension_change_between_living_frames_is_a_portal_walk(tmp_path):
    loop, store, events = loop_with(tmp_path)
    loop._note_dimension(alive("minecraft:overworld", [120.5, 64.0, 100.5]))
    loop._note_dimension(alive("minecraft:overworld", [120.6, 64.0, 100.4]))
    loop._note_dimension(alive("minecraft:the_nether", [15.5, 70.0, 12.5]))
    [link] = store.world.portals()
    assert link["from"] == {"dimension": "minecraft:overworld", "pos": [120, 64, 100]}
    assert link["to"] == {"dimension": "minecraft:the_nether", "pos": [15, 70, 12]}
    assert events[-1] == ("portal_walked", {"from_dimension": "minecraft:overworld", "from_pos": [120.6, 64.0, 100.4],
                                            "to_dimension": "minecraft:the_nether", "to_pos": [15.5, 70.0, 12.5]})


def test_a_death_or_respawn_between_frames_is_not_a_portal_walk(tmp_path):
    loop, store, _ = loop_with(tmp_path)
    loop._note_dimension(alive("minecraft:the_nether", [15.5, 70.0, 12.5]))
    loop._note_dimension({**alive("minecraft:the_nether", [15.5, 70.0, 12.5]), "health": 0})
    loop._queue_lifecycle_event(Event(seq=1, tick=1, bot="Bot", name="respawned", data={}))
    loop._note_dimension(alive("minecraft:overworld", [0.5, 70.0, 0.5]))
    # A death taken by the lifecycle task counts too.
    loop._death_count += 1
    loop._note_dimension(alive("minecraft:the_nether", [15.5, 70.0, 12.5]))
    assert store.world.portals() == []


def test_frames_carry_the_known_portals(tmp_path):
    loop, store, _ = loop_with(tmp_path)
    store.world.portal_walked("overworld", [120, 64, 100], "the_nether", [15, 70, 12])
    frame = loop._frame({"facts": alive("minecraft:the_nether", [20, 70, 12])})["facts"]
    assert frame["known_portals"][0]["to"] == {"dimension": "minecraft:the_nether", "pos": [15, 70, 12]}


def test_home_cells_are_read_only_in_the_homes_dimension(tmp_path):
    loop, _, _ = loop_with(tmp_path)
    reads = []

    class Lane:
        async def run(self, function, *args):
            return function(*args)

    loop.runtime.body = SimpleNamespace(perceive=lambda scope, params: reads.append(scope) or {
        "ok": True, "data": {"cells": []}})
    loop.runtime._read_lane = Lane()
    assert asyncio.run(loop._home_arrival(facts("minecraft:the_nether", [100, 64, 130]))) is None
    assert reads == []
    assert asyncio.run(loop._home_arrival(facts("minecraft:overworld", [100, 64, 130]))) is not None
    assert reads == ["blockCells"]
