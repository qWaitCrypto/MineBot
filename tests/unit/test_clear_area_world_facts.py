from minebot.app.client_body_tools import client_body_tools
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientTerminal


class ClearIpc:
    """A clear whose broken cells either stay open or fill with water."""

    def __init__(self, *, status, reason, after):
        self.status, self.reason, self.after = status, reason, after
        self.commanded = False

    def connect(self):
        pass

    def close(self):
        pass

    def state(self):
        return {"position": [45.7, 55.0, 655.3], "health": 20, "food": 20, "creative": False,
                "disposable_world": True, "inventory": {}, "inventory_complete": True,
                "inventory_includes_selected": True, "selected_item": "stone_pickaxe",
                "selected_item_known": True, "active_action_known": True, "active_action_id": None}

    def command(self, objective, *, action_id, timeout_ms, on_progress):
        self.commanded = True
        return RealClientTerminal(action_id, self.status, self.reason, self.state())

    def cancel(self, action_id):
        pass

    def world_read(self, scope, params):
        block = self.after if self.commanded else "minecraft:stone"
        return {"ok": True, "complete": True, "data": {"cells": [
            {"x": x, "y": y, "z": z, "type": block} for x, y, z in params["cells"]]}}


def _clear(ipc):
    tool = {t.name: t for t in client_body_tools(RealClientBody("Bot", ipc))}["clear_area"]
    result = tool.callable({"start": [46, 54, 654], "end": [44, 54, 654], "timeout_s": 30})
    return tool, result


def test_a_clear_that_times_out_in_water_says_the_cells_hold_water():
    tool, result = _clear(ClearIpc(status="timeout", reason="deadline_exceeded", after="minecraft:water"))
    assert not result.success and result.reason == "deadline_exceeded"
    construction = result.metrics["construction"]
    assert construction["expected_cells"] == 3 and construction["matched_cells"] == 0
    assert construction["newly_matching_cells"] == 0
    assert construction["remaining_by_type"] == {"minecraft:water": 3}
    seen = tool.projector(result.reason, result.metrics)
    assert seen["remaining_cell_count"] == 3
    assert seen["construction"]["remaining_by_type"] == {"minecraft:water": 3}


def test_a_finished_clear_is_verified_from_the_world():
    _, result = _clear(ClearIpc(status="completed", reason="builder_finished_verify_world", after="minecraft:air"))
    assert result.success and result.reason == "area_cleared"
    construction = result.metrics["construction"]
    assert construction["matched_cells"] == construction["newly_matching_cells"] == 3
    assert construction["remaining_by_type"] == {}


def test_a_finished_clear_with_cells_left_is_a_world_mismatch():
    _, result = _clear(ClearIpc(status="completed", reason="builder_finished_verify_world", after="minecraft:water"))
    assert not result.success and result.reason == "world_mismatch"
