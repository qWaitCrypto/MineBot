"""What the Agent is told when the Body dies and wakes again (vision-and-home-20261028.md §7.7, death notice)."""
import asyncio

from minebot.app.body_events import DEATH_DROPS, WAKING_KEYS, agent_event_data
from minebot.app.home_view import waking_notice
from minebot.contract import Event
from tests.unit.test_home_dimension import loop_with
from tests.unit.test_home_spawn import NOT_VALID
from tests.unit.test_home_view import HOME, NOW, chest, world

OVERWORLD = "minecraft:overworld"
NETHER = "minecraft:the_nether"
SPAWN = {"dimension": OVERWORLD, "pos": [0, 70, 0]}
HERE = {"dimension": OVERWORLD, "position": [100.5, 64.0, 130.2]}
CARRIED = {"iron_pickaxe": 1, "cobblestone": 40, "bread": 3}


def death(**data):
    living = {"position": [130.4, 64.0, 130.6], "dimension": OVERWORLD, "health": 3.0, "inventory": CARRIED,
              "inventory_slots": [{"slot": 0, "item": "iron_pickaxe", "count": 1}]}
    return Event(seq=7, tick=100, bot="Bot", name="death", data={
        "position": [130.5, 63.4, 130.5], "dimension": OVERWORLD, "health": 0.0, "in_lava": True, "inventory": {},
        "hurt_source": {"type": "lava", "damage_type": "minecraft:lava"}, "last_living_state": living, **data})


def test_the_death_notice_says_what_was_carried_and_what_becomes_of_drops():
    data = agent_event_data(death())
    assert data["carried"] == CARRIED
    assert data["drops"] == DEATH_DROPS == (
        "Dropped items that fall into lava or fire burn up, and those that fall into the void are lost; the rest "
        "disappear about 5 minutes after the death, if that area stays loaded the whole time.")
    assert data["position"] == [130.5, 63.4, 130.5] and data["in_lava"] is True
    assert "inventory" not in data and "inventory" not in data["last_living_state"]
    # The SDK session projects the projected event again before the Agent reads it.
    assert agent_event_data(Event(seq=7, tick=100, bot="Bot", name="death", data=data)) == data


def test_a_death_without_a_last_living_state_names_nothing_carried():
    data = agent_event_data(death(last_living_state=None))
    assert "carried" not in data and data["drops"] == DEATH_DROPS


def test_the_waking_facts_are_kept_in_the_projection():
    woke = {"position": [0.5, 70.0, 0.5], "dimension": OVERWORLD, "health": 20, "inventory_slots": [],
            **{key: f"{key} sentence" for key in WAKING_KEYS}}
    data = agent_event_data(Event(seq=8, tick=120, bot="Bot", name="respawned", data=woke))
    assert {key: data[key] for key in WAKING_KEYS} == {key: f"{key} sentence" for key in WAKING_KEYS}
    assert "inventory_slots" not in data
    assert agent_event_data(Event(seq=8, tick=120, bot="Bot", name="respawned", data=data)) == data


def test_which_point_the_body_woke_at_follows_the_games_words():
    def woke(respawn, spawn=SPAWN):
        return waking_notice({}, world(respawn=respawn, spawn=spawn), here=HERE, now=NOW)["respawn_point"]

    bed = {"dimension": OVERWORLD, "pos": [105, 64, 105], "block": "white_bed", "t": NOW - 7200}
    assert woke(bed) == ("You woke at your respawn point: the white_bed at (105, 64, 105) in the overworld, "
                         "set 2h00m ago.")
    assert woke(None) == ("The Body has seen no bed or anchor set as your respawn point, so you woke at the world "
                          "spawn (0, 70, 0) in the overworld.")
    lost = {"lost": True, "t": NOW - 5, "was": {"dimension": OVERWORLD, "pos": [105, 64, 105], "block": "white_bed"}}
    assert woke(lost) == ("The game said your bed or anchor at (105, 64, 105) was missing or blocked (just now), so "
                          "you woke at the world spawn (0, 70, 0) in the overworld.")
    gone = {**bed, "block": "bed", "gone": {"t": NOW - 120, "reason": "picked up again after sleeping"}}
    assert woke(gone, spawn=None) == ("Your respawn bed at (105, 64, 105) is no longer there (picked up again after "
                                      "sleeping, 2m ago), so you woke at the world spawn.")
    unknown = {"unknown": True, "t": NOW - 300, "said": "Respawn point set"}
    assert woke(unknown) == ("The game said your respawn point was set (5m ago), but the Body did not see at which "
                             "bed or anchor; you woke there.")


def test_home_the_way_there_and_the_death_place_from_where_the_body_woke():
    notice = waking_notice({"home": HOME}, world(), here=HERE, died=(OVERWORLD, [130.7, 64.0, 130.2]), now=NOW)
    assert notice["home"] == ("Home “the clearing house”: (100, 64, 100) in the overworld, area (95, 60, 95) to "
                              "(110, 75, 110); 30 blocks north from you.")
    assert notice["death_place"] == "You died at (130, 64, 130) in the overworld; 30 blocks east from you."
    # Died in the Nether, woke in the overworld: the way back is through the portal walked before.
    portal = {"from": {"dimension": OVERWORLD, "pos": [120, 64, 100]},
              "to": {"dimension": NETHER, "pos": [15, 70, 12]}}
    nether = waking_notice({}, world(portals=[portal]), here=HERE, died=(NETHER, [20.5, 70.0, 12.5]), now=NOW)
    assert nether["death_place"] == (
        "You died at (20, 70, 12) in the Nether; you are in the overworld; the portal at (120, 64, 100), 36 blocks "
        "north-east from you, comes out at (15, 70, 12) in the Nether, 5 blocks from there.")
    assert nether["home"] == "You have no home in this world yet." and "home_storage" not in nether
    assert "death_place" not in waking_notice({}, world(), here=HERE, now=NOW)


def test_what_home_still_holds_is_summed_over_its_containers():
    places = [chest([101, 64, 99], {"cobblestone": 64, "oak_log": 10}),
              chest([102, 64, 99], {"iron_ingot": 5, "cobblestone": 6}, complete=False),
              {"dimension": OVERWORLD, "pos": [103, 64, 103], "block": "barrel", "t": NOW - 60,
               "used": {"t": NOW - 60, "source": "read_container"}},
              {"dimension": OVERWORLD, "pos": [104, 64, 104], "block": "crafting_table", "t": NOW - 60,
               "used": {"t": NOW - 60, "source": "craft_item"}},
              chest([106, 64, 106], {"diamond": 3}, gone={"t": NOW - 30, "reason": "the block there became air"}),
              chest([300, 64, 300], {"gold_ingot": 9})]
    notice = waking_notice({"home": HOME}, world(*places), here=HERE, now=NOW)
    assert notice["home_storage"] == ("The 2 containers at home held, as last looked into: cobblestone 70, "
                                      "oak_log 10, iron_ingot 5 (1 not read in full); 1 more not looked into yet.")
    # The storage plan's containers count even before the Body looks into them.
    empty = waking_notice({"home": HOME}, world(), here=HERE, now=NOW)
    assert empty["home_storage"] == "The Body has not looked into any container at home; 2 there not looked into yet."
    bare = waking_notice({"home": {"pos": [100, 64, 100]}}, world(), here=HERE, now=NOW)
    assert bare["home_storage"] == "The Body has not looked into any container at home."


def test_waking_after_a_death_tells_the_point_home_and_the_death_place(tmp_path):
    loop, store, events = loop_with(tmp_path)
    store.update({"home": {"pos": [100, 64, 100], "dimension": "overworld"}}, expected_revision=0,
                 reason="the house by the river")
    store.world.respawn_set(OVERWORLD, [105, 64, 105], "white_bed", "Respawn point set", at=1_800_000_000.0)
    notified = []
    loop.runtime.notify_body_lifecycle_event = notified.append

    async def settle(reason):
        return {"settled": True}

    loop.runtime.cancel_active_execution_with_facts = settle
    loop._queue_lifecycle_event(death())
    # The game said the bed was gone before it sent the respawn: the respawned state carries its words.
    woke_at = {"position": [0.5, 70.0, 0.5], "dimension": OVERWORLD, "health": 20.0, "world_spawn": SPAWN,
               "spawn_message": NOT_VALID}
    loop._queue_lifecycle_event(Event(seq=8, tick=120, bot="Bot", name="respawned", data=woke_at))
    asyncio.run(loop._process_lifecycle_events())
    died, woke = notified
    assert agent_event_data(died)["carried"] == CARRIED
    seen = agent_event_data(woke)
    assert seen["respawn_point"] == ("The game said your bed or anchor at (105, 64, 105) was missing or blocked "
                                     "(just now), so you woke at the world spawn (0, 70, 0) in the overworld.")
    assert seen["home"] == "Home: (100, 64, 100) in the overworld; 141 blocks south-east, 6 down from you."
    assert seen["death_place"] == "You died at (130, 63, 130) in the overworld; 184 blocks south-east, 7 down from you."
    assert seen["home_storage"] == "The Body has not looked into any container at home."
    assert store.world.respawn()["lost"]
    assert next(kw for name, kw in events if name == "reflex_respawned")["facts"]["death_place"] == seen["death_place"]
    # A second respawn with no death seen in between names no death place.
    loop._queue_lifecycle_event(Event(seq=9, tick=140, bot="Bot", name="respawned", data=woke_at))
    asyncio.run(loop._process_lifecycle_events())
    assert "death_place" not in notified[-1].data and "respawn_point" in notified[-1].data


def test_without_a_life_store_the_respawn_is_told_as_before():
    loop, _store, _events = loop_with_no_store()
    notified = []
    loop.runtime.notify_body_lifecycle_event = notified.append
    loop._queue_lifecycle_event(Event(seq=2, tick=21, bot="Bot", name="respawned", data={"health": 20}))
    asyncio.run(loop._process_lifecycle_events())
    assert notified[0].name == "respawned" and notified[0].data == {"health": 20}


def loop_with_no_store():
    from types import SimpleNamespace

    from minebot.app.body_loop import BodyLoop
    events = []
    runtime = SimpleNamespace(trace=SimpleNamespace(emit=lambda event, **kw: events.append((event, kw))),
                              decision_client=SimpleNamespace(), last_known_body_state={},
                              execution_snapshots=SimpleNamespace(current=lambda: None))
    return BodyLoop(runtime), None, events


def test_the_death_notice_says_the_walk_gaze_went_back_to_level_only_when_it_was_on():
    from minebot.app.body_events import WALK_GAZE_RESET_ON_DEATH

    assert WALK_GAZE_RESET_ON_DEATH == (
        "Walking gaze is back to level after the death; turn look-down on again if you want it.")
    data = agent_event_data(death(walking_gaze=WALK_GAZE_RESET_ON_DEATH))
    assert data["walking_gaze"] == WALK_GAZE_RESET_ON_DEATH
    assert "walking_gaze" not in agent_event_data(death())


def test_death_notice_says_the_legs_were_reset() -> None:
    from minebot.app.body_events import MOVEMENT_RESET_ON_DEATH
    data = agent_event_data(death(movement=MOVEMENT_RESET_ON_DEATH))
    assert data["movement"] == MOVEMENT_RESET_ON_DEATH


def test_movement_ended_says_where_and_why_in_a_short_notice() -> None:
    from minebot.contract import Event
    event = Event(seq=1, tick=0, bot="b", name="movement_ended", data={
        "reason": "cannot_move", "position": [10.4, 64.0, -3.6], "dimension": "minecraft:the_end", "health": 18.0,
        "inventory_slots": [{"slot": 0}] * 40, "keep_moving": {"center": [0, 64, 0], "radius": 12, "points_visited": 9}})
    data = agent_event_data(event)
    assert "inventory_slots" not in data
    assert data["keep_moving"] == {"center": [0, 64, 0], "radius": 12, "points_visited": 9}
    assert data["message"] == ("keep_moving ended at [10, 64, -4]: the Body could not move for 10 seconds. "
                               "The Body stands now; set_movement again to move on.")
