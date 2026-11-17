"""The HOME section: the Agent's places beside the Body's own record (vision-and-home-20261028.md §7.7)."""
from minebot.app.home_view import HOME_SECTION_CHARS, NO_HOME, home_section
from minebot.app.life_profile import LifeStore
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.app.runtime_state import RuntimeScope
from minebot.brain.context import AgentContext
from minebot.brain.context_compiler import CONTEXT_PROFILES, compile_context
from tests.unit.test_agent_a6_runtime import REGION, RuntimeBody

NOW = 1_000_000.0
OVERWORLD = "minecraft:overworld"
NETHER = "minecraft:the_nether"
HOME = {"pos": [100, 64, 100], "name": "the clearing house", "dimension": OVERWORLD,
        "area": {"from": [95, 60, 95], "to": [110, 75, 110]},
        "storage": [{"pos": [101, 64, 99], "holds": ["cobblestone", "*_log"], "note": "by the door"},
                    {"pos": [102, 64, 99], "holds": ["iron_ingot"]}]}
OVERWORLD_HERE = {"dimension": "minecraft:overworld", "pos": [100.5, 64.0, 130.2]}


def chest(pos, items, *, t=NOW - 600, complete=True, dimension=OVERWORLD, **extra):
    return {"dimension": dimension, "pos": pos, "block": "chest", "items": items, "complete": complete,
            "looked": {"t": t, "source": "read_container"}, "t": t, **extra}


def world(*places, respawn=None, spawn=None, portals=()):
    return {"places": list(places), "respawn": respawn, "world_spawn": spawn, "portals": list(portals)}


def test_without_a_home_the_section_is_one_sentence():
    profile = {"outposts": [{"name": "n", "pos": [0, 64, 0], "dimension": NETHER}]}
    assert home_section(profile, world(chest([1, 64, 1], {"stone": 1})), here=OVERWORLD_HERE, now=NOW) == NO_HOME
    assert NO_HOME == "HOME: You have no home in this world yet."


def test_home_line_gives_name_place_area_and_the_way_from_here():
    text = home_section({"home": HOME}, world(), here=OVERWORLD_HERE, now=NOW)
    lines = text.splitlines()
    assert lines[0].startswith("HOME (")
    assert lines[1] == ("- Home “the clearing house”: (100, 64, 100) in the overworld, area (95, 60, 95) to "
                        "(110, 75, 110); 30 blocks north from you.")
    # A home without a written dimension is in the overworld, and the section says so.
    bare = home_section({"home": {"pos": [100, 70, 100]}}, world(), here=OVERWORLD_HERE, now=NOW)
    assert "(100, 70, 100) in the overworld (no dimension written, so the overworld); 30 blocks north, 6 up" in bare


def test_places_at_home_list_contents_plans_use_and_the_respawn_bed():
    items = {f"item_{index}": index + 1 for index in range(10)}
    places = [
        chest([101, 64, 99], items, complete=False),
        {"dimension": OVERWORLD, "pos": [104, 64, 104], "block": "crafting_table", "t": NOW - 120,
         "used": {"t": NOW - 120, "source": "craft_item"}},
        {"dimension": OVERWORLD, "pos": [105, 64, 105], "block": "white_bed", "t": NOW - 7200,
         "used": {"t": NOW - 7200, "source": "respawn point set here"}},
        {"dimension": OVERWORLD, "pos": [106, 64, 106], "block": "furnace", "t": NOW - 60,
         "gone": {"t": NOW - 60, "reason": "picked up again after smelting"}},
    ]
    respawn = {"dimension": OVERWORLD, "pos": [105, 64, 105], "block": "white_bed", "t": NOW - 7200}
    text = home_section({"home": HOME}, world(*places, respawn=respawn), here=OVERWORLD_HERE,
                        own_places=[{"dimension": "overworld", "pos": [103, 64, 103], "block": "barrel"},
                                    {"dimension": "overworld", "pos": [103, 65, 103], "block": "oak_planks"}],
                        now=NOW)
    assert "- At home:" in text
    assert ("  - chest (101, 64, 99): item_9 10, item_8 9, item_7 8, item_6 7, item_5 6, item_4 5, item_3 4, "
            "item_2 3 and 2 more kinds (not every slot read), looked 10m ago; planned for cobblestone, *_log "
            "(by the door).") in text
    assert "  - container (102, 64, 99): not looked into yet; planned for iron_ingot." in text
    assert "  - crafting_table (104, 64, 104): last used 2m ago by craft_item." in text
    assert "  - white_bed (105, 64, 105): last used 2h00m ago by respawn point set here; your respawn point." in text
    assert "  - furnace (106, 64, 106): gone 1m ago (picked up again after smelting)." in text
    assert "  - barrel (103, 64, 103): not looked into yet; you placed it." in text
    assert "oak_planks" not in text
    assert ("- Respawn: the white_bed at (105, 64, 105) in the overworld, set 2h00m ago; dying wakes you there."
            in text)


def test_other_places_are_listed_nearest_home_first_and_gone_ones_are_left_out():
    places = [chest([400, 64, 100], {"coal": 3}), chest([130, 64, 100], {"stone": 1}),
              chest([5, 70, 5], {"netherrack": 9}, dimension=NETHER),
              {**chest([150, 64, 100], {}), "gone": {"t": NOW, "reason": "the block there is air now"}}]
    text = home_section({"home": HOME}, world(*places), here=OVERWORLD_HERE, now=NOW)
    other = text.split("- Other places the Body knows:\n")[1].splitlines()
    assert other == ["  - chest (130, 64, 100), 30 blocks from home: stone 1, looked 10m ago.",
                     "  - chest (400, 64, 100), 300 blocks from home: coal 3, looked 10m ago.",
                     "  - chest (5, 70, 5) in the Nether: netherrack 9, looked 10m ago."]


def test_in_another_dimension_the_way_home_goes_through_a_known_portal():
    portal = {"from": {"dimension": OVERWORLD, "pos": [120, 64, 100]}, "to": {"dimension": NETHER, "pos": [15, 70, 12]},
              "t": NOW - 100}
    far = {"from": {"dimension": NETHER, "pos": [300, 70, 300]}, "to": {"dimension": OVERWORLD, "pos": [2400, 64, 2400]},
           "t": NOW - 50}
    here = {"dimension": "minecraft:the_nether", "pos": [20, 70, 12]}
    text = home_section({"home": HOME}, world(portals=[portal, far]), here=here, now=NOW)
    assert ("you are in the Nether; the portal at (15, 70, 12), 5 blocks west from you, comes out at "
            "(120, 64, 100) in the overworld, 20 blocks from there.") in text
    lost = home_section({"home": HOME}, world(), here=here, now=NOW)
    assert "you are in the Nether and know no portal from here to the overworld." in lost
    unknown = home_section({"home": HOME}, world(), here=None, now=NOW)
    assert "where the Body is now is not known." in unknown


def test_outposts_have_their_own_way_and_places():
    outpost = {"name": "nether base", "pos": [10, 70, 10], "dimension": NETHER,
               "storage": [{"pos": [11, 70, 10], "holds": ["gold_ingot"]}]}
    places = [chest([11, 70, 10], {"gold_ingot": 30}, dimension=NETHER)]
    text = home_section({"home": HOME, "outposts": [outpost]}, world(*places), here=OVERWORLD_HERE, now=NOW)
    assert ("- Outpost “nether base”: (10, 70, 10) in the Nether; you are in the overworld and know no portal "
            "from here to the Nether.") in text
    assert "  - chest (11, 70, 10): gold_ingot 30, looked 10m ago; planned for gold_ingot." in text


def test_the_respawn_line_follows_the_record():
    spawn = {"dimension": OVERWORLD, "pos": [0, 70, 0]}
    none = home_section({"home": HOME}, world(spawn=spawn), here=OVERWORLD_HERE, now=NOW)
    assert ("- Respawn: the Body has seen no bed or anchor set as your respawn point; dying wakes you at the world "
            "spawn (0, 70, 0) in the overworld.") in none
    gone = {"dimension": OVERWORLD, "pos": [105, 64, 105], "block": "bed", "t": NOW - 300,
            "gone": {"t": NOW - 120, "reason": "picked up again after sleeping"}}
    text = home_section({"home": HOME}, world(respawn=gone), here=OVERWORLD_HERE, now=NOW)
    assert ("- Respawn: the bed at (105, 64, 105) in the overworld, set 5m ago; it is no longer there (picked up "
            "again after sleeping, 2m ago): unless you sleep in another bed, dying wakes you at the world spawn.") in text
    lost = {"lost": True, "t": NOW - 30, "was": {"dimension": OVERWORLD, "pos": [105, 64, 105], "block": "bed"}}
    text = home_section({"home": HOME}, world(respawn=lost, spawn=spawn), here=OVERWORLD_HERE, now=NOW)
    assert ("- Respawn: just now the game said your bed or anchor at (105, 64, 105) was missing or blocked; dying "
            "wakes you at the world spawn (0, 70, 0) in the overworld.") in text


def test_belongings_say_whose_and_who_said_so():
    belongings = [{"owner": "qwait", "pos": [150, 64, 100], "dimension": OVERWORLD, "said_by": "qwait",
                   "said_at": "2026-10-28T20:00:00Z"},
                  {"owner": "shared", "area": {"from": [0, 60, 0], "to": [9, 70, 9]}, "dimension": OVERWORLD,
                   "said_by": "qwait", "said_at": "day 3", "note": "the farm we share"}]
    text = home_section({"home": HOME, "belongings": belongings}, world(), here=OVERWORLD_HERE, now=NOW)
    assert "- What players told you is theirs or shared:" in text
    assert "  - (150, 64, 100) in the overworld: qwait's, qwait said (2026-10-28T20:00:00Z)." in text
    assert ("  - the area (0, 60, 0) to (9, 70, 9) in the overworld: shared, qwait said (day 3); the farm we share."
            in text)


def test_the_section_stays_within_its_budget_other_places_going_first():
    stored = {f"item_{index}": 64 for index in range(12)}
    at_home = [chest([96 + index % 14, 61 + index // 14, 96], stored) for index in range(40)]
    elsewhere = [chest([1000 + index, 64, 1000], stored) for index in range(40)]
    text = home_section({"home": HOME}, world(*elsewhere, *at_home), here=OVERWORLD_HERE, now=NOW)
    assert len(text) <= HOME_SECTION_CHARS
    assert "- Other places the Body knows:\n  - ... 40 more not shown." in text
    assert text.count("  - chest") >= 5 and "more not shown." in text.split("- At home:")[1]


def test_the_agent_context_carries_home_after_the_ladder():
    context = AgentContext(system_prompt="SP", goal_text="", language="English")
    context.observe_ladder("LADDER (your plan): - empty")
    context.observe_home("HOME: You have no home in this world yet.")
    for profile in ("full", "social"):
        text = compile_context(profile, context.snapshot_facts()).text
        assert text.index("LADDER (your plan)") < text.index("HOME: You have no home")
    assert "home" not in CONTEXT_PROFILES["terse"]


def test_the_composed_runtime_refreshes_home_every_model_call(tmp_path):
    life = LifeStore(tmp_path, RuntimeScope("server", "world", "Bot1"))
    parts = build_phase1_agent_runtime(body=RuntimeBody(), goal_text="", model_provider=None,
                                       config=Phase1RuntimeConfig(natural_region=REGION, life_store=life))
    assert NO_HOME in compile_context("full", parts.context.snapshot_facts()).text
    life.update({"home": {"pos": [3, 64, 4], "name": "camp"}}, expected_revision=0, reason="a home")
    parts.runtime.last_known_body_state = {"dimension": "minecraft:overworld", "pos": [3.5, 64.0, 10.5]}
    for refresh in parts.runtime.context_refreshers:
        refresh(parts.context)
    text = compile_context("full", parts.context.snapshot_facts()).text
    assert "- Home “camp”: (3, 64, 4) in the overworld (no dimension written, so the overworld); 6 blocks north" in text
