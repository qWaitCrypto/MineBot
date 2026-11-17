"""The Body's own record of its places (vision-and-home-20261028.md §7.7)."""
import json

from minebot.app import world_record as wr
from minebot.app.life_profile import LifeStore
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.app.runtime_state import RuntimeScope
from minebot.app.world_record import WorldRecord, observe_tool_result
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolSidecar, WeldContext, execute_tool
from minebot.contract import ToolResult
from tests.unit.test_agent_a6_runtime import REGION, RuntimeBody

NETHER = "minecraft:the_nether"
OVERWORLD = "minecraft:overworld"


def record(tmp_path):
    return WorldRecord(tmp_path / "bot.world.jsonl")


def rows(tmp_path):
    return [json.loads(line) for line in (tmp_path / "bot.world.jsonl").read_text().splitlines()]


def test_a_container_look_is_kept_by_place_and_survives_a_restart(tmp_path):
    world = record(tmp_path)
    world.container_seen("overworld", [10.7, 64, -3.2], {"minecraft:cobblestone": 40, "coal": 3},
                         complete=True, source="read_container", block="minecraft:chest")
    world.container_seen(OVERWORLD, [12, 64, -4], {"oak_log": 0, "stick": 2}, complete=False,
                         source="transfer_container_item")

    again = record(tmp_path)
    # Newest first.
    assert [place["pos"] for place in again.snapshot()["places"]] == [[12, 64, -4], [10, 64, -4]]
    chest = again.place("overworld", [10, 64, -4])
    assert chest["block"] == "chest" and chest["items"] == {"cobblestone": 40, "coal": 3} and chest["complete"]
    assert chest["dimension"] == OVERWORLD and chest["looked"]["source"] == "read_container"
    # A container whose block was not named is still a container.
    barrel = again.place(OVERWORLD, [12, 64, -4])
    assert barrel["block"] == "container" and barrel["items"] == {"stick": 2} and not barrel["complete"]


def test_places_are_folded_by_dimension_and_cell(tmp_path):
    world = record(tmp_path)
    world.container_seen("overworld", [1, 64, 2], {"stone": 5}, complete=True, source="read_container")
    world.container_seen(NETHER, [1, 64, 2], {"netherrack": 9}, complete=True, source="read_container")
    world.container_seen("minecraft:overworld", [1.4, 64.9, 2.2], {"stone": 7}, complete=True,
                         source="transfer_container_item")

    places = record(tmp_path).snapshot()["places"]
    assert len(places) == 2
    assert record(tmp_path).place("overworld", [1, 64, 2])["items"] == {"stone": 7}
    assert record(tmp_path).place("nether", [1, 64, 2])["items"] == {"netherrack": 9}


def test_a_place_with_unknown_dimension_or_position_is_not_written(tmp_path):
    world = record(tmp_path)
    world.container_seen(None, [1, 64, 2], {"stone": 5}, complete=True, source="read_container")
    world.container_seen(OVERWORLD, [1, 64], {"stone": 5}, complete=True, source="read_container")
    world.place_used(OVERWORLD, [1, 64, 2], "stone", "craft_item")
    assert world.snapshot()["places"] == []
    assert not (tmp_path / "bot.world.jsonl").exists()


def test_workstations_and_beds_are_places_with_the_tool_that_used_them(tmp_path):
    world = record(tmp_path)
    world.place_used(OVERWORLD, [5, 64, 5], "minecraft:crafting_table", "craft_item")
    world.place_used(OVERWORLD, [6, 64, 5], "red_bed", "go_to_bed")
    world.place_used(OVERWORLD, [7, 64, 5], "bed", "place_bed_and_sleep")

    table = world.place(OVERWORLD, [5, 64, 5])
    assert table["block"] == "crafting_table" and table["used"]["source"] == "craft_item"
    assert world.place(OVERWORLD, [6, 64, 5])["block"] == "red_bed"
    assert world.place(OVERWORLD, [7, 64, 5])["block"] == "bed"


def test_a_gone_place_is_marked_and_comes_back_when_seen_again(tmp_path):
    world = record(tmp_path)
    world.place_gone(OVERWORLD, [5, 64, 5], "never known")
    assert not (tmp_path / "bot.world.jsonl").exists()

    world.place_used(OVERWORLD, [5, 64, 5], "furnace", "smelt_item")
    world.place_gone(OVERWORLD, [5, 64, 5], "picked up again after smelting")
    assert record(tmp_path).place(OVERWORLD, [5, 64, 5])["gone"]["reason"] == "picked up again after smelting"

    world.place_used(OVERWORLD, [5, 64, 5], "furnace", "smelt_item")
    assert "gone" not in record(tmp_path).place(OVERWORLD, [5, 64, 5])


def test_the_respawn_point_follows_the_game_and_goes_with_its_bed(tmp_path):
    world = record(tmp_path)
    world.respawn_set(OVERWORLD, [6, 64, 5], "white_bed", "block.minecraft.set_spawn")
    respawn = record(tmp_path).snapshot()["respawn"]
    assert respawn["pos"] == [6, 64, 5] and respawn["block"] == "white_bed" and "gone" not in respawn
    # The bed is a place too.
    assert world.place(OVERWORLD, [6, 64, 5])["block"] == "white_bed"

    world.place_gone(OVERWORLD, [6, 64, 5], "picked up again after sleeping")
    assert record(tmp_path).snapshot()["respawn"]["gone"]["reason"] == "picked up again after sleeping"

    # A bed placed again in the same cell is the respawn point again.
    world.place_used(OVERWORLD, [6, 64, 5], "white_bed", "go_to_bed")
    assert "gone" not in record(tmp_path).snapshot()["respawn"]

    world.respawn_lost("block.minecraft.spawn.not_valid")
    lost = record(tmp_path).snapshot()["respawn"]
    assert lost["lost"] and lost["said"] == "block.minecraft.spawn.not_valid"
    assert lost["was"] == {"dimension": OVERWORLD, "pos": [6, 64, 5], "block": "white_bed"}


def test_the_world_spawn_is_written_only_when_it_changes(tmp_path):
    world = record(tmp_path)
    world.world_spawn_seen("overworld", [0, 70, 0])
    world.world_spawn_seen(OVERWORLD, [0.2, 70.5, 0.9])
    world.world_spawn_seen(OVERWORLD, [16, 70, 0])
    assert len(rows(tmp_path)) == 2
    assert record(tmp_path).snapshot()["world_spawn"]["pos"] == [16, 70, 0]


def test_portal_walks_pair_both_ends_and_merge_the_same_portal(tmp_path):
    world = record(tmp_path)
    world.portal_walked(OVERWORLD, [100, 64, 100], NETHER, [12, 70, 12])
    # Back through the same portal, a step to the side.
    world.portal_walked(NETHER, [13, 70, 12], OVERWORLD, [101, 64, 100])
    # Not a dimension change.
    world.portal_walked(OVERWORLD, [0, 64, 0], OVERWORLD, [5, 64, 5])
    world.portal_walked(None, [0, 64, 0], NETHER, [5, 64, 5])

    portals = record(tmp_path).snapshot()["portals"]
    assert len(portals) == 1
    assert portals[0]["from"] == {"dimension": OVERWORLD, "pos": [100, 64, 100]}
    assert portals[0]["to"] == {"dimension": NETHER, "pos": [12, 70, 12]}


def test_portals_and_places_are_bounded_gone_places_leave_first(tmp_path, monkeypatch):
    monkeypatch.setattr(wr, "KEPT_PLACES", 3)
    monkeypatch.setattr(wr, "KEPT_PORTALS", 2)
    world = record(tmp_path)
    world.respawn_set(OVERWORLD, [-1, 64, 0], "red_bed", "block.minecraft.set_spawn")
    for x in range(2):
        world.place_used(OVERWORLD, [x, 64, 0], "furnace", "smelt_item")
    world.place_gone(OVERWORLD, [1, 64, 0], "picked up again after smelting")
    world.place_used(OVERWORLD, [8, 64, 0], "furnace", "smelt_item")
    world.place_used(OVERWORLD, [9, 64, 0], "furnace", "smelt_item")
    # The gone furnace leaves, then the one untouched longest; the respawn bed stays though it is the oldest.
    assert [place["pos"][0] for place in record(tmp_path).snapshot()["places"]] == [9, 8, -1]

    for x in range(3):
        world.portal_walked(OVERWORLD, [x * 100, 64, 0], NETHER, [x * 100 // 8, 70, 0])
    assert [link["from"]["pos"][0] for link in record(tmp_path).snapshot()["portals"]] == [100, 200]


def test_the_file_is_compacted_to_its_folded_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(wr, "COMPACT_AFTER_ROWS", 10)
    world = record(tmp_path)
    world.respawn_set(OVERWORLD, [6, 64, 5], "white_bed", "block.minecraft.set_spawn")
    world.world_spawn_seen(OVERWORLD, [0, 70, 0])
    world.portal_walked(OVERWORLD, [100, 64, 100], NETHER, [12, 70, 12])
    for count in range(12):
        world.container_seen(OVERWORLD, [1, 64, 2], {"stone": count + 1}, complete=True, source="read_container")
    before = world.snapshot()

    # 16 rows were written; the compacted file holds the 5 folded ones and the 5 since.
    assert len(rows(tmp_path)) == 10
    assert record(tmp_path).snapshot() == before


def test_broken_lines_are_skipped_on_load(tmp_path):
    world = record(tmp_path)
    world.place_used(OVERWORLD, [5, 64, 5], "furnace", "smelt_item")
    with (tmp_path / "bot.world.jsonl").open("a") as handle:
        handle.write('{"kind": "used", "t": 1\n["not a row"]\n{"kind": "container", "t": 2}\n')
    assert [place["pos"] for place in record(tmp_path).snapshot()["places"]] == [[5, 64, 5]]


def chest_slots(*items):
    return [{"slot": index, "item": item, "count": count, "empty": False} for index, (item, count) in enumerate(items)]


def test_tool_results_feed_containers_workstations_and_beds(tmp_path):
    world = record(tmp_path)
    observe_tool_result(world, "read_container", {"pos": [1, 64, 2]}, {
        "success": True, "reason": "container_read",
        "metrics": {"pos": [1, 64, 2], "slots": chest_slots(("minecraft:iron_ingot", 5), ("minecraft:iron_ingot", 3)),
                    "complete": True}}, OVERWORLD)
    observe_tool_result(world, "transfer_container_item", {"item": "coal", "count": 4}, ToolResult(
        True, "completed", False, metrics={"container_pos": [3, 64, 2], "container_type": "barrel",
                                           "container_items": {"coal": 4}, "container_complete": True}), OVERWORLD)
    observe_tool_result(world, "craft_item", {}, {"success": True, "metrics": {
        "workspace": {"mode": "existing_table", "table_pos": [5, 64, 2], "table_type": "crafting_table"}}}, OVERWORLD)
    observe_tool_result(world, "smelt_item", {}, {"success": True, "metrics": {
        "furnace_pos": [6, 64, 2], "furnace_type": "blast_furnace", "reclaim": {"success": True}}}, OVERWORLD)
    observe_tool_result(world, "go_to_bed", {}, {"success": True, "reason": "sleeping", "metrics": {
        "target": {"pos": [7, 64, 2], "type": "white_bed"}}}, OVERWORLD)
    observe_tool_result(world, "place_bed_and_sleep", {}, {"success": False, "reason": "bed_not_recovered", "metrics": {
        "bed_placed": True, "bed_recovered": False, "bed_position": [8, 64, 2]}}, OVERWORLD)

    assert world.place(OVERWORLD, [1, 64, 2])["items"] == {"iron_ingot": 8}
    assert world.place(OVERWORLD, [3, 64, 2])["block"] == "barrel"
    assert world.place(OVERWORLD, [5, 64, 2])["used"]["source"] == "craft_item"
    # A standing furnace's ``reclaim`` takes its items back; the furnace stays.
    furnace = world.place(OVERWORLD, [6, 64, 2])
    assert furnace["block"] == "blast_furnace" and "gone" not in furnace
    assert world.place(OVERWORLD, [7, 64, 2])["block"] == "white_bed"
    assert world.place(OVERWORLD, [8, 64, 2])["block"] == "bed"


def test_tables_furnaces_and_beds_picked_up_again_are_gone(tmp_path):
    world = record(tmp_path)
    for pos in ([5, 64, 2], [6, 64, 2], [8, 64, 2]):
        world.place_used(OVERWORLD, pos, "crafting_table", "craft_item")
    observe_tool_result(world, "craft_item", {}, {"success": True, "metrics": {
        "workspace": {"mode": "temporary_table", "table_pos": [5, 64, 2]}, "reclaim": {"success": True}}}, OVERWORLD)
    observe_tool_result(world, "smelt_with_temporary_furnace", {}, {"success": True, "metrics": {
        "furnace_pos": [6, 64, 2], "furnace_item": "furnace", "reclaim": {"success": True}}}, OVERWORLD)
    observe_tool_result(world, "place_bed_and_sleep", {}, {"success": True, "metrics": {
        "bed_placed": True, "bed_recovered": True, "bed_position": [8, 64, 2]}}, OVERWORLD)
    for pos in ([5, 64, 2], [6, 64, 2], [8, 64, 2]):
        assert "gone" in world.place(OVERWORLD, pos), pos


def test_an_incomplete_read_is_kept_as_not_every_slot(tmp_path):
    world = record(tmp_path)
    observe_tool_result(world, "read_container", {"pos": [1, 64, 2]}, {
        "success": False, "reason": "container_read_incomplete",
        "metrics": {"pos": [1, 64, 2], "slots": chest_slots(("coal", 3))}}, OVERWORLD)
    seen = world.place(OVERWORLD, [1, 64, 2])
    assert seen["items"] == {"coal": 3} and seen["complete"] is False


def test_a_place_taken_down_in_a_world_edit_receipt_is_gone(tmp_path):
    world = record(tmp_path)
    world.place_used(NETHER, [4, 70, 4], "crafting_table", "craft_item")
    world.place_used(OVERWORLD, [4, 70, 4], "crafting_table", "craft_item")
    receipt = {"action_id": "a1", "dimension": NETHER, "edits": [
        {"pos": [4, 70, 4], "before": "minecraft:crafting_table", "after": "minecraft:air", "op": "break"},
        {"pos": [5, 70, 4], "before": "minecraft:stone", "after": "minecraft:air", "op": "break"}]}
    # The receipt can sit inside an inner action's metrics.
    observe_tool_result(world, "mine_block", {}, {"success": True, "metrics": {
        "original_result": {"metrics": {"world_edit_receipt": receipt}}}}, OVERWORLD)
    assert world.place(NETHER, [4, 70, 4])["gone"]["reason"] == "the block there became air"
    assert "gone" not in world.place(OVERWORLD, [4, 70, 4])


def test_results_that_name_no_place_change_nothing(tmp_path):
    world = record(tmp_path)
    observe_tool_result(world, "read_container", {"pos": [1, 64, 2]}, {
        "success": False, "reason": "perception_failed", "metrics": {"pos": [1, 64, 2]}}, OVERWORLD)
    observe_tool_result(world, "transfer_container_item", {}, {"success": False, "reason": "container_denied",
                                                              "metrics": {"container_pos": [1, 64, 2]}}, OVERWORLD)
    observe_tool_result(world, "smelt_item", {}, {"success": False, "metrics": {"furnace_pos": [1, 64, 2]}}, OVERWORLD)
    observe_tool_result(world, "move_to", {}, {"success": True, "metrics": {"target": {"pos": [1, 64, 2]}}}, OVERWORLD)
    observe_tool_result(world, "read_life", {}, "not a mapping", OVERWORLD)
    assert world.snapshot()["places"] == []


def test_a_container_that_became_another_block_is_gone(tmp_path):
    world = record(tmp_path)
    world.container_seen(OVERWORLD, [1, 64, 2], {"stone": 5}, complete=True, source="read_container")
    observe_tool_result(world, "transfer_container_item", {}, {"success": False, "reason": "container_wrong_type",
                        "metrics": {"container_pos": [1, 64, 2], "container_type": "air"}}, OVERWORLD)
    assert world.place(OVERWORLD, [1, 64, 2])["gone"]["reason"] == "the block there is air now"


def test_the_registry_shows_read_results_to_its_observer():
    seen = []
    tool = RegisteredTool("read_container", "", {}, lambda params: ToolResult(True, "container_read", False, metrics={}),
                          ToolSidecar("read_container", mutating=False))
    context = WeldContext(body=RuntimeBody(), authority=ProgressAuthority(), goal_text="",
                          read_result_observer=lambda *call: seen.append(call))
    payload = execute_tool(tool, {"pos": [1, 64, 2]}, context)
    assert seen == [("read_container", {"pos": [1, 64, 2]}, payload)]


def test_the_composed_runtime_feeds_tool_results_into_the_life_store_world(tmp_path):
    life = LifeStore(tmp_path, RuntimeScope("server", "world", "Bot1"))
    parts = build_phase1_agent_runtime(body=RuntimeBody(), goal_text="", model_provider=None,
                                       config=Phase1RuntimeConfig(natural_region=REGION, life_store=life))
    parts.runtime.last_known_body_state = {"dimension": "the_nether"}
    weld = parts.runtime.weld_context
    weld.read_result_observer("read_container", {"pos": [1, 64, 2]}, {"success": True, "reason": "container_read", "metrics": {
        "pos": [1, 64, 2], "slots": chest_slots(("gold_ingot", 2)), "complete": True}})
    weld.raw_result_observer("transfer_container_item", {}, ToolResult(True, "completed", False, metrics={
        "container_pos": [3, 64, 2], "container_items": {"coal": 1}, "container_complete": True}))

    assert life.world.path == life.path.with_suffix(".world.jsonl")
    assert life.world.place(NETHER, [1, 64, 2])["items"] == {"gold_ingot": 2}
    assert life.world.place(NETHER, [3, 64, 2])["items"] == {"coal": 1}
