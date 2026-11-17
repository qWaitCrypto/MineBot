"""Storing by the Agent's plan and taking by the Body's record (vision-and-home-20261028.md §7.7, Body)."""
from types import SimpleNamespace

from minebot.app.world_record import WorldRecord, observe_tool_result
from minebot.body.storage import StorageTransactions, matches
from minebot.contract import PerceptionResult, ToolResult

OVERWORLD = "minecraft:overworld"
CHEST_A, CHEST_B, FAR = (10, 64, 10), (12, 64, 10), (100, 64, 100)
HOME = {"name": "hut", "pos": [11, 64, 11], "dimension": "minecraft:overworld",
        "storage": [{"pos": list(CHEST_A), "holds": ["cobblestone", "*_ore"]}, {"pos": list(CHEST_B), "holds": ["dirt"]}]}
CAMP = {"name": "camp", "pos": [500, 70, 500], "dimension": "minecraft:the_nether", "storage": []}


def slot(index, item=None, count=0):
    return {"slot": index, "empty": item is None, "count": count, **({"item": f"minecraft:{item}"} if item else {})}


class Body:
    bot_name = "Bot1"

    def __init__(self, carried, dimension=OVERWORLD):
        self.slots = [slot(index, *carried[index]) if index in carried else slot(index) for index in range(46)]
        self.state = SimpleNamespace(pos=(11.0, 64.0, 11.0), dimension=dimension)

    def get_state(self):
        return self.state

    def perceive(self, scope, params):
        assert scope == "inventory"
        return PerceptionResult(bot="Bot1", scope="inventory", type="perception", ok=True, complete=True,
                                data={"slots": self.slots}, uncertainty=[], next=None, error=None)


class Containers:
    """The verified transfer, as far as these tools see it: what moved and what the container holds now."""

    def __init__(self, held=None):
        self.held = {pos: dict(items) for pos, items in (held or {}).items()}
        self.calls = []

    def transfer_item(self, pos, *, item, count, direction, total_slots, timeout_s):
        self.calls.append((tuple(pos), item, count, direction))
        inside = self.held.setdefault(tuple(pos), {})
        moved = count if direction == "bot_to_container" else min(count, inside.get(item, 0))
        inside[item] = inside.get(item, 0) + (moved if direction == "bot_to_container" else -moved)
        return ToolResult(success=moved >= count, reason="completed" if moved >= count else "partial", can_retry=False,
                          metrics={"container_pos": list(pos), "moved_count": moved, "container_type": "chest",
                                   "container_items": {name: n for name, n in inside.items() if n > 0},
                                   "container_complete": True})


def storage(body, containers, *, profile=None, record=None):
    profile = {"home": HOME, "outposts": [CAMP]} if profile is None else profile
    return StorageTransactions(body, containers, navigator=None, profile=lambda: profile,
                               places=lambda: record.snapshot()["places"] if record else [],
                               dimension=lambda: body.state.dimension)


CARRIED = {0: ("stone_pickaxe", 1), 1: ("cobblestone", 64), 2: ("cobblestone", 20), 3: ("iron_ore", 5),
           4: ("oak_log", 10), 5: ("dirt", 3), 36: ("iron_helmet", 1)}


def test_everything_but_what_is_kept_goes_where_the_plan_says(tmp_path):
    containers = Containers()
    result = storage(Body(CARRIED), containers).store_items(keep=["stone_pickaxe", "*_log"])
    assert result.success and result.reason == "completed"
    assert containers.calls == [(CHEST_A, "cobblestone", 84, "bot_to_container"),
                                (CHEST_A, "iron_ore", 5, "bot_to_container"), (CHEST_B, "dirt", 3, "bot_to_container")]
    # Worn armour is not in the 36 slots: neither stored nor listed as kept.
    assert result.metrics["kept"] == ["oak_log", "stone_pickaxe"] and result.metrics["not_stored"] == []
    assert result.metrics["place"] == "home"
    # The world record takes what each container holds now from the result.
    record = WorldRecord(tmp_path / "w.world.jsonl")
    observe_tool_result(record, "store_items", {}, result, OVERWORLD)
    assert record.place(OVERWORLD, CHEST_A)["items"] == {"cobblestone": 84, "iron_ore": 5}
    assert record.place(OVERWORLD, CHEST_B)["items"] == {"dirt": 3}


def test_what_no_planned_container_holds_stays_and_is_reported():
    containers = Containers()
    result = storage(Body({0: ("cobblestone", 10), 1: ("rotten_flesh", 4)}), containers).store_items(keep=[])
    assert not result.success and result.reason == "stored_part"
    assert result.metrics["not_stored"] == [
        {"item": "rotten_flesh", "count": 4, "why": "no container in the home's storage plan holds it"}]
    assert containers.calls == [(CHEST_A, "cobblestone", 10, "bot_to_container")]
    only = Containers()
    assert storage(Body(CARRIED), only).store_items(keep=[], only=["*_ore"]).success
    assert only.calls == [(CHEST_A, "iron_ore", 5, "bot_to_container")]
    nothing = storage(Body({0: ("stone_pickaxe", 1)}), Containers()).store_items(keep=["*"])
    assert nothing.success and nothing.reason == "nothing_to_store"


def test_take_items_from_the_containers_the_record_knows_largest_first(tmp_path):
    record = WorldRecord(tmp_path / "w.world.jsonl")
    record.container_seen(OVERWORLD, CHEST_A, {"iron_ingot": 5}, complete=True, source="read_container", block="chest")
    record.container_seen(OVERWORLD, CHEST_B, {"iron_ingot": 20}, complete=True, source="read_container", block="chest")
    # Away from home: the record knows it, but it is not one of the home's containers.
    record.container_seen(OVERWORLD, FAR, {"diamond": 3}, complete=True, source="read_container", block="chest")
    containers = Containers({CHEST_A: {"iron_ingot": 5}, CHEST_B: {"iron_ingot": 20}, FAR: {"diamond": 3}})
    result = storage(Body({}), containers, record=record).take_items(items={"iron_ingot": 22, "diamond": 1})
    assert containers.calls == [(CHEST_B, "iron_ingot", 20, "container_to_bot"),
                                (CHEST_A, "iron_ingot", 2, "container_to_bot")]
    assert not result.success and result.reason == "took_part"
    assert result.metrics["short"] == [
        {"item": "diamond", "count": 1, "why": "the Body's record knows no container here holding it"}]
    # A container that gave less than the record said is reported too.
    containers.held[CHEST_A]["iron_ingot"] = 0
    record.container_seen(OVERWORLD, CHEST_A, {"iron_ingot": 3}, complete=True, source="read_container", block="chest")
    short = storage(Body({}), containers, record=record).take_items(items={"iron_ingot": 3})
    assert short.metrics["short"] == [{"item": "iron_ingot", "count": 3, "why": "the containers that held it gave less"}]


def test_the_place_must_be_known_and_in_this_dimension():
    body = Body(CARRIED, dimension="minecraft:the_nether")
    refused = storage(body, Containers()).store_items(keep=[])
    assert refused.reason == "place_in_another_dimension"
    assert refused.metrics["place_dimension"] == OVERWORLD and refused.metrics["body_dimension"] == "minecraft:the_nether"
    # The outpost there is found by name, and a Body that does not say its dimension is taken at its word.
    assert storage(body, Containers()).store_items(keep=[], place="camp").metrics["place"] == "camp"
    body.state.dimension = None
    assert storage(body, Containers()).store_items(keep=["*"]).reason == "nothing_to_store"
    assert storage(body, Containers()).store_items(keep=[], place="mine").metrics["outposts"] == ["camp"]
    assert storage(body, Containers(), profile={}).take_items(items={"dirt": 1}).reason == "no_home"
    broken = StorageTransactions(body, Containers(), navigator=None, profile=lambda: 1 / 0, places=list,
                                 dimension=lambda: None)
    assert broken.store_items(keep=[]).reason == "life_profile_unreadable"


def test_patterns_ignore_the_namespace():
    assert matches("minecraft:oak_log", ["*_log"]) and matches("oak_log", ["minecraft:oak_log"])
    assert not matches("oak_planks", ["*_log"])
