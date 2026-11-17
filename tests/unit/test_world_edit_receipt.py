from minebot.app.runner import _model_tool_payload
from minebot.contract import Action
from minebot.game.real_client_body import WORLD_EDIT_RECEIPT, RealClientBody
from minebot.game.real_client_ipc import RealClientTerminal


def _edit(index, pos, before, after, op):
    return {"index": index, "pos": pos, "before": before, "after": after, "op": op,
            "action_kind": "build", "target": True, "tick": index}


class ReceiptIpc:
    def __init__(self, *, frames, count, truncated=False, unconfirmed=0):
        self.frames, self.count, self.truncated, self.unconfirmed = frames, count, truncated, unconfirmed

    def connect(self):
        pass

    def close(self):
        pass

    def state(self):
        return {"position": [0.5, 64.0, 0.5], "dimension": "minecraft:overworld", "health": 20, "food": 20,
                "creative": False, "disposable_world": True, "inventory": {}, "inventory_complete": True,
                "inventory_includes_selected": True, "selected_item": "", "selected_item_known": True,
                "active_action_known": True, "active_action_id": None}

    def command(self, objective, *, action_id, timeout_ms, on_progress):
        for edits in self.frames:
            on_progress({"type": "progress", "action_id": action_id, "lite": True, "world_edits": edits})
        return RealClientTerminal(action_id, "completed", "builder_finished_verify_world", {
            **self.state(), "world_edit_count": self.count, "world_edits_truncated": self.truncated,
            "world_edits_unconfirmed": self.unconfirmed})

    def cancel(self, action_id):
        pass


def _run(ipc):
    body = RealClientBody("Bot", ipc)
    action = Action.create("clearArea", {"start": [0, 64, 0], "end": [1, 64, 0], "timeout_s": 10})
    assert body.execute(action).accepted
    return body.await_action_terminal(action.id, timeout_s=5).data[WORLD_EDIT_RECEIPT]


def test_the_receipt_holds_every_edit_of_the_action_across_frames():
    receipt = _run(ReceiptIpc(frames=[[_edit(0, [0, 64, 0], "stone", "air", "break")],
                                      [_edit(1, [1, 64, 0], "stone", "air", "break")]], count=2))
    assert [edit["index"] for edit in receipt["edits"]] == [0, 1]
    assert receipt["complete"] is True and receipt["dimension"] == "minecraft:overworld"


def test_a_missing_frame_or_the_cell_cap_makes_the_receipt_incomplete():
    lost = _run(ReceiptIpc(frames=[[_edit(0, [0, 64, 0], "stone", "air", "break")]], count=2))
    assert lost["complete"] is False and lost["expected_edits"] == 2
    capped = _run(ReceiptIpc(frames=[[_edit(0, [0, 64, 0], "stone", "air", "break")]], count=1, truncated=True))
    assert capped["complete"] is False and capped["truncated"] is True


def test_the_model_never_reads_the_receipt():
    payload = _model_tool_payload("clear_area", {"success": True, "reason": "area_cleared", "metrics": {
        "matched_cells": 2, WORLD_EDIT_RECEIPT: {"edits": [_edit(0, [0, 64, 0], "stone", "air", "break")]}}},
        trace_ref="trace:1")
    text = repr(payload)
    assert WORLD_EDIT_RECEIPT not in text and "matched_cells" in text


def test_a_change_the_server_has_not_confirmed_makes_the_receipt_incomplete():
    # House run 2026-10-18: a clear ended on the client's view of the last
    # break, before the server confirmed it; one of two cells had a receipt.
    receipt = _run(ReceiptIpc(frames=[[_edit(0, [0, 64, 0], "diorite", "air", "break")]], count=1, unconfirmed=1))
    assert receipt["complete"] is False and receipt["unconfirmed_edits"] == 1
