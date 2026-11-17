"""The game's own words on the respawn point reach the world record (vision-and-home-20261028.md §7.7 item 5)."""
from minebot.app.home_view import home_section
from minebot.app.life_profile import LifeStore
from minebot.app.runtime_state import RuntimeScope
from tests.unit.test_home_dimension import loop_with

OVERWORLD = "minecraft:overworld"
SET = {"seq": 1, "key": "block.minecraft.set_spawn", "text": "Respawn point set", "at_ms": 1_800_000_000_000,
       "dimension": OVERWORLD, "pos": [6, 64, 5], "block": "white_bed"}
NOT_VALID = {"seq": 2, "key": "block.minecraft.spawn.not_valid", "at_ms": 1_800_000_600_000, "dimension": OVERWORLD,
             "text": "You have no home bed or charged respawn anchor, or it was obstructed"}


def frame(**facts):
    return {"facts": {"dimension": OVERWORLD, "position": [0.5, 64.0, 0.5], "health": 20,
                      "world_spawn": {"dimension": OVERWORLD, "pos": [0, 70, 0]}, **facts}}


def test_the_games_words_on_the_respawn_point_are_kept_with_the_time_it_said_them(tmp_path):
    loop, store, events = loop_with(tmp_path)
    loop._frame(frame(spawn_message=SET))
    respawn = store.world.snapshot()["respawn"]
    assert respawn["pos"] == [6, 64, 5] and respawn["block"] == "white_bed" and respawn["t"] == 1_800_000_000.0
    assert respawn["said"] == "Respawn point set"
    assert store.world.place(OVERWORLD, [6, 64, 5])["used"]["source"] == "respawn point set here"
    assert store.world.snapshot()["world_spawn"]["pos"] == [0, 70, 0]
    assert ("respawn_message", {"key": SET["key"], "pos": [6, 64, 5], "block": "white_bed"}) in events
    # Every later frame still carries the same message and world spawn: nothing more is written.
    revision = store.world.revision
    loop._frame(frame(spawn_message=SET))
    assert store.world.revision == revision


def test_no_home_bed_at_respawn_marks_the_point_lost(tmp_path):
    loop, store, _ = loop_with(tmp_path)
    loop._frame(frame(spawn_message=SET))
    loop._frame(frame(spawn_message=NOT_VALID))
    lost = store.world.snapshot()["respawn"]
    assert lost["lost"] and lost["t"] == 1_800_000_600.0
    assert lost["was"] == {"dimension": OVERWORLD, "pos": [6, 64, 5], "block": "white_bed"}


def test_a_message_written_before_a_restart_is_not_written_again(tmp_path):
    loop, store, _ = loop_with(tmp_path)
    loop._frame(frame(spawn_message=NOT_VALID))
    again, store_again, _ = loop_with(tmp_path)
    revision = store_again.world.revision
    again._frame(frame(spawn_message=NOT_VALID))
    assert store_again.world.revision == revision
    # A newer one after the restart is written.
    again._frame(frame(spawn_message={**SET, "seq": 1, "at_ms": 1_800_000_900_000}))
    assert store_again.world.snapshot()["respawn"]["pos"] == [6, 64, 5]


def test_set_at_a_block_the_body_did_not_see_does_not_keep_the_old_bed(tmp_path):
    loop, store, _ = loop_with(tmp_path)
    loop._frame(frame(spawn_message=SET))
    loop._frame(frame(spawn_message={key: value for key, value in SET.items() if key not in ("pos", "block")}
                      | {"seq": 2, "at_ms": 1_800_000_300_000}))
    unknown = store.world.snapshot()["respawn"]
    assert unknown["unknown"] and "pos" not in unknown
    # It survives a reload, and the HOME section says what the Body does not know.
    reloaded = LifeStore(tmp_path, RuntimeScope("s", "w", "Bot")).world.snapshot()
    assert reloaded["respawn"]["unknown"]
    text = home_section({"home": {"pos": [0, 64, 0]}}, reloaded, here={"dimension": OVERWORLD, "pos": [0, 64, 0]},
                        now=1_800_000_360.0)
    assert "the game said your respawn point was set, but the Body did not see at which bed or anchor" in text
    loop._frame(frame(spawn_message={**NOT_VALID, "seq": 3, "at_ms": 1_800_000_400_000}))
    assert store.world.snapshot()["respawn"]["was"] is None

