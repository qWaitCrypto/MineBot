from minebot.app.body_capability_tools import AREA_READ_MAX_CELLS, _read_area, area_observation_projector
from minebot.contract import PerceptionResult


class AreaBody:
    bot_name = "Bot1"

    def __init__(self, world):
        self.world = world
        self.reads = 0

    def perceive(self, scope, params):
        assert scope == "blockCells"
        self.reads += 1
        cells = [{"x": x, "y": y, "z": z, "type": self.world.get((x, y, z), "minecraft:air")}
                 for x, y, z in params["cells"]]
        return PerceptionResult(bot="Bot1", scope=scope, type="perception", ok=True, complete=True,
                                data={"cells": cells})


def test_an_area_is_read_in_one_call_grouped_by_kind_and_checked_against_expect():
    world = {(0, 64, 0): "minecraft:oak_planks", (1, 64, 0): "minecraft:oak_planks", (0, 64, 1): "minecraft:dirt"}
    body = AreaBody(world)
    result = _read_area(body, {"start": [1, 64, 1], "end": [0, 64, 0], "expect": "oak_planks"})

    assert result.success and result.reason == "area_read"
    assert body.reads == 1
    assert result.metrics["cell_count"] == 4 and result.metrics["complete"] is True
    assert result.metrics["expect"] == {"block": "minecraft:oak_planks", "matched": 2, "not_matched": 2, "unread": 0}
    shown = area_observation_projector(result.reason, result.metrics)
    kinds = {group["type"]: group["positions"] for group in shown["blocks"]}
    assert sorted(kinds["minecraft:oak_planks"]) == [[0, 64, 0], [1, 64, 0]]
    assert kinds["minecraft:dirt"] == [[0, 64, 1]] and kinds["minecraft:air"] == [[1, 64, 1]]
    assert shown["positions_complete"] is True


def test_a_large_area_answer_keeps_every_kind_and_count():
    world = {(x, 64, z): f"minecraft:block_{(x * 7 + z) % 40}" for x in range(8) for z in range(8)}
    world.update({(x, 65, z): "minecraft:stone" for x in range(8) for z in range(8)})
    result = _read_area(AreaBody(world), {"start": [0, 64, 0], "end": [7, 71, 7]})
    shown = area_observation_projector(result.reason, result.metrics)

    assert result.metrics["cell_count"] == 512
    counts = {group["type"]: group.get("count", len(group.get("positions", []))) for group in shown["blocks"]}
    assert sum(counts.values()) == 512
    assert counts["minecraft:stone"] == 64


def test_an_area_beyond_the_bound_is_refused_with_the_bound():
    body = AreaBody({})
    result = _read_area(body, {"start": [0, 0, 0], "end": [9, 9, 9]})
    assert not result.success and result.reason == "area_too_large"
    assert result.metrics["requested_cells"] == 1000 and result.metrics["max_cells"] == AREA_READ_MAX_CELLS
    assert body.reads == 0
